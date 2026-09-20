package com.akshat.jarvisremote

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import okhttp3.*
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * One WebSocket connection to the PC's remote_control.py server.
 * Mirrors the protocol defined there:
 *   -> {"type":"auth","token":...}
 *   -> {"type":"tap"|"drag_start"|"drag_move"|"drag_end","x":0..1,"y":0..1}
 *   -> {"type":"chat","text":...,"mode":"type"|"dictate"}
 *   <- {"type":"auth_ok"|"auth_fail"}
 *   <- {"type":"frame","data":"<base64 jpeg>"}
 *   <- {"type":"chat_reply","text":...,"speak":true|false}
 */
class RemoteConnection(
    private val url: String,
    private val token: String,
    private val callbacks: Callbacks,
) {
    interface Callbacks {
        fun onAuthOk()
        fun onAuthFail()
        fun onFrame(bitmap: Bitmap)
        fun onChatReply(text: String, shouldSpeak: Boolean)
        fun onConnectionError(message: String)
    }

    private var webSocket: WebSocket? = null
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS) // keep alive for streaming frames
        .build()

    fun connect() {
        val request = Request.Builder().url(url).build()
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                val auth = JSONObject().apply {
                    put("type", "auth")
                    put("token", token)
                }
                webSocket.send(auth.toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleMessage(text)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                callbacks.onConnectionError(t.message ?: "connection failed")
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                callbacks.onConnectionError("connection closed: $reason")
            }
        })
    }

    fun disconnect() {
        webSocket?.close(1000, "closing")
        webSocket = null
    }

    private fun handleMessage(text: String) {
        val json = try {
            JSONObject(text)
        } catch (e: Exception) {
            return
        }

        when (json.optString("type")) {
            "auth_ok" -> callbacks.onAuthOk()
            "auth_fail" -> callbacks.onAuthFail()
            "frame" -> {
                val data = json.optString("data")
                val bytes = Base64.decode(data, Base64.DEFAULT)
                val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                if (bitmap != null) callbacks.onFrame(bitmap)
            }
            "chat_reply" -> {
                callbacks.onChatReply(json.optString("text"), json.optBoolean("speak", false))
            }
        }
    }

    // ---- outgoing messages ----

    fun sendTap(xNorm: Float, yNorm: Float) = sendInput("tap", xNorm, yNorm)
    fun sendDragStart(xNorm: Float, yNorm: Float) = sendInput("drag_start", xNorm, yNorm)
    fun sendDragMove(xNorm: Float, yNorm: Float) = sendInput("drag_move", xNorm, yNorm)
    fun sendDragEnd(xNorm: Float, yNorm: Float) = sendInput("drag_end", xNorm, yNorm)

    private fun sendInput(type: String, xNorm: Float, yNorm: Float) {
        val msg = JSONObject().apply {
            put("type", type)
            put("x", xNorm.toDouble())
            put("y", yNorm.toDouble())
        }
        webSocket?.send(msg.toString())
    }

    /** mode: "type" (text only reply) or "dictate" (spoken reply too) */
    fun sendChat(text: String, mode: String) {
        val msg = JSONObject().apply {
            put("type", "chat")
            put("text", text)
            put("mode", mode)
        }
        webSocket?.send(msg.toString())
    }
}
