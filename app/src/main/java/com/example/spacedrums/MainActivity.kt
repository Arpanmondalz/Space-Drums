package com.example.spacedrums

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.SoundPool
import android.net.wifi.WifiManager
import android.os.Bundle
import android.util.Log
import android.util.Size
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import com.google.mediapipe.tasks.components.containers.NormalizedLandmark
import com.google.mediapipe.tasks.vision.poselandmarker.PoseLandmarkerResult
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

// --- LOGIC ENGINE ---
class DrumLogic {
    // MATCHING PYTHON ZONES EXACTLY
    private val CYMBAL_HEIGHT = 0.35f
    private val DIVIDER_1 = 0.35f
    private val DIVIDER_2 = 0.65f

    private val STICK_EXTENSION = 1.5f

    // --- KALMAN FILTER (PYTHON MATCH) ---
    // Slightly higher alpha (0.7 vs 0.6) makes it stickier to the hand
    private val KALMAN_ALPHA = 0.7f
    private val KALMAN_BETA = 0.2f

    // CRITICAL FIX: Python uses 4 frames at 60fps (~66ms).
    // Android runs slower (~30fps). We must LOWER this to 2.0
    // to prevent the "overshoot" ghost hits.
    private val PREDICTION_FACTOR = 2.0f

    var currentZoneLeft = "SNARE"
    var currentZoneRight = "SNARE"

    // State: [x, y, vx, vy]
    private var kLeft = FloatArray(4)
    private var kRight = FloatArray(4)
    private var initLeft = false
    private var initRight = false

    fun update(lElbow: NormalizedLandmark, lWrist: NormalizedLandmark, rElbow: NormalizedLandmark, rWrist: NormalizedLandmark): Pair<FloatArray, FloatArray> {
        val (rawLx, rawLy) = extendLine(lElbow.x(), lElbow.y(), lWrist.x(), lWrist.y())
        val (rawRx, rawRy) = extendLine(rElbow.x(), rElbow.y(), rWrist.x(), rWrist.y())

        val leftTip = runKalman(rawLx, rawLy, true)
        val rightTip = runKalman(rawRx, rawRy, false)

        currentZoneLeft = getZone(leftTip[0], leftTip[1])
        currentZoneRight = getZone(rightTip[0], rightTip[1])
        return Pair(leftTip, rightTip)
    }

    private fun extendLine(x1: Float, y1: Float, x2: Float, y2: Float): Pair<Float, Float> {
        val x = x2 + (x2 - x1) * STICK_EXTENSION
        val y = y2 + (y2 - y1) * STICK_EXTENSION
        return Pair(x, y)
    }

    private fun runKalman(rawX: Float, rawY: Float, isLeft: Boolean): FloatArray {
        val k = if (isLeft) kLeft else kRight

        if (!(if (isLeft) initLeft else initRight)) {
            k[0] = rawX; k[1] = rawY; k[2] = 0f; k[3] = 0f
            if (isLeft) initLeft = true else initRight = true
            return floatArrayOf(rawX, rawY)
        }

        // 1. Prediction
        val predX = k[0] + k[2]
        val predY = k[1] + k[3]

        // 2. Residual
        val resX = rawX - predX
        val resY = rawY - predY

        // 3. Update (Alpha-Beta Filter)
        k[0] = predX + (KALMAN_ALPHA * resX) // Position
        k[1] = predY + (KALMAN_ALPHA * resY)
        k[2] = k[2] + (KALMAN_BETA * resX)   // Velocity
        k[3] = k[3] + (KALMAN_BETA * resY)

        // 4. Projection (The "Lag Fix")
        // We project slightly into the future to reduce visual lag,
        // but clamped to prevent the "floor tom ghost hit".
        val finalX = k[0] + (k[2] * PREDICTION_FACTOR)
        val finalY = k[1] + (k[3] * PREDICTION_FACTOR)

        return floatArrayOf(finalX.coerceIn(0f, 1f), finalY.coerceIn(0f, 1f))
    }

    private fun getZone(x: Float, y: Float): String {
        if (y < CYMBAL_HEIGHT) {
            return if (x < DIVIDER_2) "CRASH" else "RIDE"
        } else {
            if (x < DIVIDER_1) return "HI-HAT"
            if (x < DIVIDER_2) return "SNARE"
            return "FLOOR TOM"
        }
    }
}

// --- MAIN ACTIVITY ---
class MainActivity : ComponentActivity(), PoseDetectorHelper.PoseDetectorListener {

    private lateinit var cameraExecutor: ExecutorService
    private lateinit var poseHelper: PoseDetectorHelper
    private val drumLogic = DrumLogic()
    private lateinit var soundPool: SoundPool

    private val soundMap = mutableMapOf<String, Int>()

    private val volumeMap = mutableStateMapOf(
        "SNARE" to 1f, "HI-HAT" to 1f, "FLOOR TOM" to 1f,
        "KICK" to 1f, "CRASH" to 1f, "RIDE" to 1f
    )

    // Debounce
    private val DEBOUNCE_TIME = 40L
    private var lastHitTime = mutableMapOf("LEFT" to 0L, "RIGHT" to 0L, "KICK" to 0L)

    private val UDP_HIT_PORT = 5556
    private val UDP_DISCOVERY_PORT = 5555
    private var isRunning = true

    // UI State
    private var leftStickPos by mutableStateOf(Offset.Zero)
    private var rightStickPos by mutableStateOf(Offset.Zero)
    private var leftZoneName by mutableStateOf("WAITING")
    private var rightZoneName by mutableStateOf("WAITING")

    private val requestPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted -> if (isGranted) startApp() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        cameraExecutor = Executors.newSingleThreadExecutor()
        poseHelper = PoseDetectorHelper(this, this)
        initAudio()

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            startApp()
        } else {
            requestPermissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    private fun startApp() {
        setContent { SpaceDrumsApp() }
        Thread { udpLoop() }.start()
        startCameraAnalysis()
    }

    private fun initAudio() {
        val audioAttributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_GAME)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
            .build()
        soundPool = SoundPool.Builder().setMaxStreams(10).setAudioAttributes(audioAttributes).build()

        try {
            soundMap["SNARE"] = soundPool.load(this, R.raw.snare, 1)
            soundMap["KICK"] = soundPool.load(this, R.raw.kick, 1)
            soundMap["HI-HAT"] = soundPool.load(this, R.raw.hihat, 1)
            soundMap["FLOOR TOM"] = soundPool.load(this, R.raw.tom, 1)
            soundMap["CRASH"] = soundPool.load(this, R.raw.crash, 1)
            soundMap["RIDE"] = soundPool.load(this, R.raw.ride, 1)
        } catch (e: Exception) { Log.e("SpaceDrums", "Sound Error", e) }
    }

    private fun playSound(zone: String) {
        val soundId = soundMap[zone] ?: return
        val vol = volumeMap[zone] ?: 1f
        soundPool.play(soundId, vol, vol, 1, 0, 1f)
    }

    private fun udpLoop() {
        val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val lock = wifi.createMulticastLock("SpaceDrumsLock")
        lock.setReferenceCounted(true)
        lock.acquire()

        val broadcastSocket = DatagramSocket()
        broadcastSocket.broadcast = true

        try {
            val socket = DatagramSocket(UDP_HIT_PORT)
            socket.broadcast = true
            socket.soTimeout = 5

            val buffer = ByteArray(64)
            val packet = DatagramPacket(buffer, buffer.size)
            var lastBroadcast = 0L

            while (isRunning) {
                val now = System.currentTimeMillis()

                if (now - lastBroadcast > 1000) {
                    broadcastAggressive(broadcastSocket, "AIRDRUM_SERVER")
                    lastBroadcast = now
                }

                while (true) {
                    try {
                        socket.receive(packet)
                        val msg = String(packet.data, 0, packet.length).trim().uppercase()
                        val hitNow = System.currentTimeMillis()

                        if (msg.contains("KICK")) {
                            val last = lastHitTime["KICK"] ?: 0L
                            if (hitNow - last > DEBOUNCE_TIME) {
                                playSound("KICK")
                                lastHitTime["KICK"] = hitNow
                            }
                        }

                        if (msg.contains("LEFT")) {
                            val last = lastHitTime["LEFT"] ?: 0L
                            if (hitNow - last > DEBOUNCE_TIME) {
                                playSound(drumLogic.currentZoneLeft)
                                lastHitTime["LEFT"] = hitNow
                            }
                        }

                        if (msg.contains("RIGHT")) {
                            val last = lastHitTime["RIGHT"] ?: 0L
                            if (hitNow - last > DEBOUNCE_TIME) {
                                playSound(drumLogic.currentZoneRight)
                                lastHitTime["RIGHT"] = hitNow
                            }
                        }
                    } catch (e: java.net.SocketTimeoutException) {
                        break
                    } catch (e: Exception) {
                        break
                    }
                }
                Thread.sleep(1)
            }
        } catch (e: Exception) {
            Log.e("UDP", "Error", e)
        } finally {
            if (lock.isHeld) lock.release()
            broadcastSocket.close()
        }
    }

    private fun broadcastAggressive(socket: DatagramSocket, msg: String) {
        try {
            val data = msg.toByteArray()
            try {
                socket.send(DatagramPacket(data, data.size, InetAddress.getByName("255.255.255.255"), UDP_DISCOVERY_PORT))
            } catch (e: Exception) {}
            try {
                val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
                val dhcp = wifi.dhcpInfo
                val broadcast = (dhcp.ipAddress and dhcp.netmask) or dhcp.netmask.inv()
                val quads = ByteArray(4)
                for (k in 0..3) quads[k] = ((broadcast shr k * 8) and 0xFF).toByte()
                socket.send(DatagramPacket(data, data.size, InetAddress.getByAddress(quads), UDP_DISCOVERY_PORT))
            } catch (e: Exception) {}
        } catch (e: Exception) {}
    }

    private fun startCameraAnalysis() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)
        cameraProviderFuture.addListener({
            val provider = cameraProviderFuture.get()
            val analyzer = ImageAnalysis.Builder()
                .setTargetResolution(Size(640, 360))
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888).build()
                .also { it.setAnalyzer(cameraExecutor) { img -> poseHelper.detectLiveStream(img) } }

            try {
                provider.unbindAll()
                provider.bindToLifecycle(this, CameraSelector.DEFAULT_FRONT_CAMERA, analyzer)
            } catch(e: Exception) { Log.e("Cam", "Bind failed", e) }
        }, ContextCompat.getMainExecutor(this))
    }

    override fun onResults(result: PoseLandmarkerResult) {
        result.landmarks().firstOrNull()?.let { lm ->
            val (lTip, rTip) = drumLogic.update(lm[14], lm[16], lm[13], lm[15])
            leftStickPos = Offset(lTip[0], lTip[1])
            rightStickPos = Offset(rTip[0], rTip[1])
            leftZoneName = drumLogic.currentZoneLeft
            rightZoneName = drumLogic.currentZoneRight
        }
    }

    override fun onError(error: String) { }

    @Composable
    fun SpaceDrumsApp() {
        Box(modifier = Modifier.fillMaxSize().background(Color.Black)) {
            DrumOverlay()
            MixerUI(volumeMap, modifier = Modifier.align(Alignment.CenterEnd))

            // --- BRANDING LOGO ---
            Column(
                modifier = Modifier.align(Alignment.TopCenter).padding(top = 20.dp),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Image(
                    painter = painterResource(id = R.drawable.logo),
                    contentDescription = "App Logo",
                    modifier = Modifier.height(60.dp),
                    contentScale = ContentScale.Fit
                )
            }
        }
    }

    @Composable
    fun DrumOverlay() {
        Canvas(modifier = Modifier.fillMaxSize()) {
            val w = size.width; val h = size.height
            val cymbalY = h * 0.35f

            // MATCHING PYTHON ZONES
            val div1 = w * 0.35f
            val div2 = w * 0.65f

            drawLine(Color.Magenta, Offset(0f, cymbalY), Offset(w, cymbalY), 3f)
            drawLine(Color.Green, Offset(div1, cymbalY), Offset(div1, h), 3f)
            drawLine(Color.Green, Offset(div2, 0f), Offset(div2, h), 3f)

            drawCircle(Color.Cyan, 30f, Offset(leftStickPos.x * w, leftStickPos.y * h))
            drawCircle(Color.Red, 30f, Offset(rightStickPos.x * w, rightStickPos.y * h))
        }
        Column(modifier = Modifier.padding(16.dp)) {
            Text("L: $leftZoneName", color = Color.Cyan, fontSize = 24.sp, fontWeight = FontWeight.Bold)
            Text("R: $rightZoneName", color = Color.Red, fontSize = 24.sp, fontWeight = FontWeight.Bold)
        }
    }

    @Composable
    fun MixerUI(volumes: MutableMap<String, Float>, modifier: Modifier = Modifier) {
        Column(modifier = modifier.background(Color(0x88000000)).padding(16.dp)) {
            volumes.keys.forEach { k ->
                val v = volumes[k] ?: 1f
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(k, color = Color.White, fontSize = 10.sp, modifier = Modifier.width(60.dp))
                    Slider(value = v, onValueChange = { volumes[k] = it }, modifier = Modifier.width(100.dp))
                }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        isRunning = false
        cameraExecutor.shutdown()
        soundPool.release()
    }
}