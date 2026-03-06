package com.example.spacedrums

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.camera2.CaptureRequest
import android.media.AudioAttributes
import android.media.SoundPool
import android.net.wifi.WifiManager
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.util.Range
import android.util.Size
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.annotation.OptIn
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
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
import com.google.mediapipe.tasks.vision.poselandmarker.PoseLandmarkerResult
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

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

    private var showLowLightWarning by mutableStateOf(false)

    // INCREASED DEBOUNCE to 120ms to prevent physical stick double-hits
    private val DEBOUNCE_TIME = 100L
    private var lastHitTime = mutableMapOf("LEFT" to 0L, "RIGHT" to 0L, "KICK" to 0L)

    private val UDP_HIT_PORT = 5556
    private val UDP_DISCOVERY_PORT = 5555
    private var isRunning = true

    // UI State
    private var leftStickPos by mutableStateOf(Offset.Zero)
    private var rightStickPos by mutableStateOf(Offset.Zero)
    private var leftZoneName by mutableStateOf("WAITING")
    private var rightZoneName by mutableStateOf("WAITING")

    // UI Drawing State for dynamic lines
    private var uiCymbalY by mutableStateOf(0.35f)
    private var uiLeftDiv by mutableStateOf(0.35f)
    private var uiRightDiv by mutableStateOf(0.65f)
    private var uiChestCenter by mutableStateOf(0.5f)

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
                // Ground truth timestamp for hardware sync
                val now = SystemClock.uptimeMillis()

                if (now - lastBroadcast > 1000) {
                    broadcastAggressive(broadcastSocket, "AIRDRUM_SERVER")
                    lastBroadcast = now
                }

                while (true) {
                    try {
                        socket.receive(packet)
                        val msg = String(packet.data, 0, packet.length).trim().uppercase()

                        // Exact time the packet was received, matching camera's clock
                        val hitNow = SystemClock.uptimeMillis()

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
                                // Request dynamic trajectory prediction based on latency
                                val predictedZone = drumLogic.predictHitZone(isLeft = true, hitTimestamp = hitNow)
                                leftZoneName = predictedZone
                                playSound(predictedZone)
                                lastHitTime["LEFT"] = hitNow
                            }
                        }

                        if (msg.contains("RIGHT")) {
                            val last = lastHitTime["RIGHT"] ?: 0L
                            if (hitNow - last > DEBOUNCE_TIME) {
                                // Request dynamic trajectory prediction based on latency
                                val predictedZone = drumLogic.predictHitZone(isLeft = false, hitTimestamp = hitNow)
                                rightZoneName = predictedZone
                                playSound(predictedZone)
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

    @OptIn(ExperimentalCamera2Interop::class)
    private fun startCameraAnalysis() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)
        cameraProviderFuture.addListener({
            val provider = cameraProviderFuture.get()

            val analyzerBuilder = ImageAnalysis.Builder()
                .setTargetResolution(Size(640, 360))
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)

            // HARDWARE OVERRIDE: Request 60 FPS from the camera sensor
            Camera2Interop.Extender(analyzerBuilder).setCaptureRequestOption(
                CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, Range(30, 60)
            )

            val analyzer = analyzerBuilder.build().also {
                it.setAnalyzer(cameraExecutor) { img -> poseHelper.detectLiveStream(img) }
            }

            try {
                provider.unbindAll()
                // SWITCH TO REAR CAMERA
                provider.bindToLifecycle(this, CameraSelector.DEFAULT_FRONT_CAMERA, analyzer)
            } catch(e: Exception) { Log.e("Cam", "Bind failed", e) }
        }, ContextCompat.getMainExecutor(this))
    }

    override fun onResults(result: PoseLandmarkerResult) {
        result.landmarks().firstOrNull()?.let { lm ->
            // Extract MediaPipe's exact frame timestamp
            val frameTime = result.timestampMs()

            // Update the flight recorder in DrumLogic
            drumLogic.updateTracking(
                lm[11], lm[12], // Left/Right Shoulder
                lm[14], lm[16], // Left Elbow/Wrist
                lm[13], lm[15], // Right Elbow/Wrist
                frameTime
            )

            // Update the UI warning state
            showLowLightWarning = drumLogic.isLowLight

            // Read latest positions purely for UI drawing
            leftStickPos = Offset(drumLogic.latestLeftX, drumLogic.latestLeftY)
            rightStickPos = Offset(drumLogic.latestRightX, drumLogic.latestRightY)

            // Sync UI bounds to match DrumLogic
            uiCymbalY = drumLogic.uiCymbalHeight
            uiLeftDiv = drumLogic.uiLeftDivider
            uiRightDiv = drumLogic.uiRightDivider
            uiChestCenter = drumLogic.uiChestX
        }
    }

    override fun onError(error: String) { }

    @Composable
    fun SpaceDrumsApp() {
        Box(modifier = Modifier.fillMaxSize().background(Color.Black)) {
            DrumOverlay()

            // LOW LIGHT WARNING
            if (showLowLightWarning) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Color.Red.copy(alpha = 0.7f))
                        .padding(8.dp)
                        .align(Alignment.TopCenter)
                ) {
                    Text(
                        text = "⚠️ Low light detected! Turn on some lights for better tracking.",
                        color = Color.White,
                        fontSize = 14.sp,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.align(Alignment.Center)
                    )
                }
            }

            MixerUI(volumeMap, modifier = Modifier.align(Alignment.CenterEnd))

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
            val w = size.width
            val h = size.height

            // Dynamic Body-Anchored Zones mapped to screen pixels
            val cymbalPixelY = uiCymbalY * h
            val div1PixelX = uiLeftDiv * w
            val div2PixelX = uiRightDiv * w
            val chestPixelX = uiChestCenter * w

            // Crash/Ride Splitter (above cymbal line)
            drawLine(Color.Yellow, Offset(chestPixelX, 0f), Offset(chestPixelX, cymbalPixelY), 3f)
            // Cymbal Horizon
            drawLine(Color.Magenta, Offset(0f, cymbalPixelY), Offset(w, cymbalPixelY), 3f)
            // Hi-Hat / Snare / Tom Vertical Dividers
            drawLine(Color.Green, Offset(div1PixelX, cymbalPixelY), Offset(div1PixelX, h), 3f)
            drawLine(Color.Green, Offset(div2PixelX, cymbalPixelY), Offset(div2PixelX, h), 3f)

            // Stick Tips
            drawCircle(Color.Cyan, 30f, Offset(leftStickPos.x * w, leftStickPos.y * h))
            drawCircle(Color.Red, 30f, Offset(rightStickPos.x * w, rightStickPos.y * h))
        }
        Column(modifier = Modifier.padding(16.dp)) {
            // Zone names now show what drum was hit LAST, effectively verifying the prediction engine
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