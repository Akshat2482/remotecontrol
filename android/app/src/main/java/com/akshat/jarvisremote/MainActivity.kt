package com.akshat.jarvisremote

import android.content.Intent
import android.graphics.Bitmap
import android.os.Bundle
import android.speech.RecognizerIntent
import android.speech.tts.TextToSpeech
import android.view.MotionEvent
import android.view.View
import android.widget.*
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import java.util.Locale
import kotlin.math.abs

class MainActivity : AppCompatActivity(), RemoteConnection.Callbacks {

    private lateinit var connectBar: View
    private lateinit var urlInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var connectButton: Button
    private lateinit var statusText: TextView

    private lateinit var chatList: RecyclerView
    private lateinit var chatInput: EditText
    private lateinit var sendButton: ImageButton
    private lateinit var micButton: ImageButton

    private lateinit var screenView: ImageView
    private lateinit var screenPlaceholder: TextView

    private lateinit var chatAdapter: ChatAdapter
    private val messages = mutableListOf<ChatMessage>()

    private var connection: RemoteConnection? = null
    private var tts: TextToSpeech? = null

    // touch -> tap/drag state
    private var touchDownX = 0f
    private var touchDownY = 0f
    private var isDragging = false
    private val dragThresholdPx = 24f

    private val speechLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val spoken = result.data
            ?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            ?.firstOrNull()
        if (!spoken.isNullOrBlank()) {
            sendMessage(spoken, mode = "dictate")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        connectBar = findViewById(R.id.connectBar)
        urlInput = findViewById(R.id.urlInput)
        tokenInput = findViewById(R.id.tokenInput)
        connectButton = findViewById(R.id.connectButton)
        statusText = findViewById(R.id.statusText)

        chatList = findViewById(R.id.chatList)
        chatInput = findViewById(R.id.chatInput)
        sendButton = findViewById(R.id.sendButton)
        micButton = findViewById(R.id.micButton)

        screenView = findViewById(R.id.screenView)
        screenPlaceholder = findViewById(R.id.screenPlaceholder)

        chatAdapter = ChatAdapter(messages)
        chatList.layoutManager = LinearLayoutManager(this)
        chatList.adapter = chatAdapter

        tts = TextToSpeech(this) { }

        connectButton.setOnClickListener { attemptConnect() }
        sendButton.setOnClickListener { onSendClicked() }
        micButton.setOnClickListener { onMicClicked() }

        screenView.setOnTouchListener { view, event -> handleScreenTouch(view, event) }
    }

    // ---------------- Connection ----------------

    private fun attemptConnect() {
        val url = urlInput.text.toString().trim()
        val token = tokenInput.text.toString().trim()
        if (url.isEmpty() || token.isEmpty()) {
            statusText.text = "Enter both the server URL and token"
            return
        }

        statusText.text = "Connecting..."
        connection = RemoteConnection(url, token, this)
        connection?.connect()
    }

    override fun onAuthOk() = runOnUiThread {
        statusText.text = "Connected"
        connectBar.visibility = View.GONE
    }

    override fun onAuthFail() = runOnUiThread {
        statusText.text = "Auth failed — check the token"
    }

    override fun onConnectionError(message: String) = runOnUiThread {
        statusText.text = "Disconnected: $message"
        connectBar.visibility = View.VISIBLE
    }

    override fun onFrame(bitmap: Bitmap) = runOnUiThread {
        screenPlaceholder.visibility = View.GONE
        screenView.setImageBitmap(bitmap)
    }

    // ---------------- Chat ----------------

    private fun onSendClicked() {
        val text = chatInput.text.toString().trim()
        if (text.isEmpty()) return
        chatInput.setText("")
        sendMessage(text, mode = "type")
    }

    private fun onMicClicked() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault())
            putExtra(RecognizerIntent.EXTRA_PROMPT, "Speak to Jarvis...")
        }
        try {
            speechLauncher.launch(intent)
        } catch (e: Exception) {
            statusText.text = "Speech recognition unavailable on this device"
        }
    }

    /** mode: "type" -> text-only reply, "dictate" -> Jarvis also speaks the reply */
    private fun sendMessage(text: String, mode: String) {
        chatAdapter.addMessage(ChatMessage(text, fromUser = true))
        chatList.scrollToPosition(messages.size - 1)
        connection?.sendChat(text, mode)
    }

    override fun onChatReply(text: String, shouldSpeak: Boolean) = runOnUiThread {
        chatAdapter.addMessage(ChatMessage(text, fromUser = false))
        chatList.scrollToPosition(messages.size - 1)

        // Only speak the reply for dictate-mode messages — type mode is
        // text-only, per the requirement that Jarvis shouldn't talk back
        // unless you used the mic.
        if (shouldSpeak) {
            tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, null)
        }
    }

    // ---------------- Screen touch -> tap/drag ----------------

    private fun handleScreenTouch(view: View, event: MotionEvent): Boolean {
        val xNorm = (event.x / view.width).coerceIn(0f, 1f)
        val yNorm = (event.y / view.height).coerceIn(0f, 1f)

        when (event.action) {
            MotionEvent.ACTION_DOWN -> {
                touchDownX = event.x
                touchDownY = event.y
                isDragging = false
            }
            MotionEvent.ACTION_MOVE -> {
                val moved = abs(event.x - touchDownX) > dragThresholdPx ||
                    abs(event.y - touchDownY) > dragThresholdPx
                if (moved && !isDragging) {
                    isDragging = true
                    connection?.sendDragStart(xNorm, yNorm)
                } else if (isDragging) {
                    connection?.sendDragMove(xNorm, yNorm)
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                if (isDragging) {
                    connection?.sendDragEnd(xNorm, yNorm)
                } else {
                    connection?.sendTap(xNorm, yNorm)
                }
                isDragging = false
            }
        }
        return true
    }

    override fun onDestroy() {
        super.onDestroy()
        connection?.disconnect()
        tts?.shutdown()
    }
}
