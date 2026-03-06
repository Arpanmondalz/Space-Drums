package com.example.spacedrums

import com.google.mediapipe.tasks.components.containers.NormalizedLandmark
import kotlin.math.max

class DrumLogic {
    private val STICK_EXTENSION = 1.5f

    private val KALMAN_ALPHA = 0.7f
    private val KALMAN_BETA = 0.2f
    private val PREDICTION_FACTOR = 2.0f
    private val CONFIDENCE_THRESHOLD = 0.4f

    @Volatile var isLowLight = false
    private var lastFrameTime = 0L

    // UI Drawing Coordinates (Tracked for the real-time visualizer)
    @Volatile var uiCymbalHeight = 0.35f
    @Volatile var uiLeftDivider = 0.35f
    @Volatile var uiRightDivider = 0.65f
    @Volatile var uiChestX = 0.5f

    // Track latest raw positions for the UI sticks
    @Volatile var latestLeftX = 0.5f
    @Volatile var latestLeftY = 0.5f
    @Volatile var latestRightX = 0.5f
    @Volatile var latestRightY = 0.5f

    // FIXED KIT DIMENSIONS
    private val SNARE_HALF_WIDTH = 0.20f
    private val CYMBAL_OFFSET_Y = 0.20f

    private var kLeft = FloatArray(4)
    private var kRight = FloatArray(4)
    private var initLeft = false
    private var initRight = false

    // Increased to 15 frames (250ms at 60fps) to ensure we capture the highest point of the arm swing
    private val HISTORY_SIZE = 15

    // Thread-safe flight data logs
    private val leftHistory = ArrayDeque<FrameData>()
    private val rightHistory = ArrayDeque<FrameData>()
    private val lock = Any()

    private data class FrameData(
        val x: Float, val y: Float,
        val chestX: Float, val chestY: Float,
        val timestamp: Long
    )

    // Runs at 60FPS on the Camera Thread
    fun updateTracking(
        lShoulder: NormalizedLandmark, rShoulder: NormalizedLandmark,
        lElbow: NormalizedLandmark, lWrist: NormalizedLandmark,
        rElbow: NormalizedLandmark, rWrist: NormalizedLandmark,
        frameTimestamp: Long
    ) {
        // CHECK FOR LOW LIGHT / LOW FPS
        if (lastFrameTime != 0L) {
            val frameGap = frameTimestamp - lastFrameTime
            // If gap is > 60ms, the camera is likely struggling with exposure/light
            isLowLight = frameGap > 60
        }
        lastFrameTime = frameTimestamp
        val chestX = (lShoulder.x() + rShoulder.x()) / 2f
        val chestY = (lShoulder.y() + rShoulder.y()) / 2f

        uiChestX = chestX
        uiCymbalHeight = chestY - CYMBAL_OFFSET_Y
        uiLeftDivider = chestX - SNARE_HALF_WIDTH
        uiRightDivider = chestX + SNARE_HALF_WIDTH

        val lConf = Math.min(lElbow.visibility().orElse(1f), lWrist.visibility().orElse(1f))
        val rConf = Math.min(rElbow.visibility().orElse(1f), rWrist.visibility().orElse(1f))

        val (rawLx, rawLy) = extendLine(lElbow.x(), lElbow.y(), lWrist.x(), lWrist.y())
        val (rawRx, rawRy) = extendLine(rElbow.x(), rElbow.y(), rWrist.x(), rWrist.y())

        val leftTip = runKalman(rawLx, rawLy, lConf, true)
        val rightTip = runKalman(rawRx, rawRy, rConf, false)

        latestLeftX = leftTip[0]; latestLeftY = leftTip[1]
        latestRightX = rightTip[0]; latestRightY = rightTip[1]

        synchronized(lock) {
            leftHistory.addLast(FrameData(leftTip[0], leftTip[1], chestX, chestY, frameTimestamp))
            rightHistory.addLast(FrameData(rightTip[0], rightTip[1], chestX, chestY, frameTimestamp))

            if (leftHistory.size > HISTORY_SIZE) leftHistory.removeFirst()
            if (rightHistory.size > HISTORY_SIZE) rightHistory.removeFirst()
        }
    }

    // Runs exactly when the UDP hit arrives on the Network Thread
    fun predictHitZone(isLeft: Boolean, hitTimestamp: Long): String {
        val historyCopy = synchronized(lock) {
            val src = if (isLeft) leftHistory else rightHistory
            if (src.size < 3) return "SNARE" // Fallback if we don't have enough data
            src.toList()
        }

        val latest = historyCopy.last()

        // Grab a frame from ~50ms ago to calculate the terminal "whip" velocity
        val pastFrame = historyCopy[kotlin.math.max(0, historyCopy.size - 3)]

        val dtMs = (latest.timestamp - pastFrame.timestamp).toFloat()

        if (dtMs <= 0f) {
            return getDynamicZone(latest.x, latest.y, latest.chestX, latest.chestY)
        }

        // --- Calculate Instantaneous Velocity (Pixels per Millisecond) ---
        val vx = (latest.x - pastFrame.x) / dtMs
        val vy = (latest.y - pastFrame.y) / dtMs

        val networkLatencyMs = 20f
        val pipelineLatencyMs = kotlin.math.max(0L, hitTimestamp - latest.timestamp).toFloat()
        val totalLatencyMs = (pipelineLatencyMs + networkLatencyMs).coerceAtMost(250f)

        // --- AXIS-INDEPENDENT TUNING ---
        // Keep horizontal prediction at 100% for snappy left/right transitions
        val X_PREDICTION_MULTIPLIER = 1.0f

        // Choke vertical prediction down to 25%. This severely limits how far a
        // rebound can push the prediction upward into the cymbal zones.
        val Y_PREDICTION_MULTIPLIER = 0.25f

        // --- Extrapolate ---
        val predictedX = latest.x + (vx * totalLatencyMs * X_PREDICTION_MULTIPLIER)
        val predictedY = latest.y + (vy * totalLatencyMs * Y_PREDICTION_MULTIPLIER)

        return getDynamicZone(predictedX, predictedY, latest.chestX, latest.chestY)
    }

    private fun extendLine(x1: Float, y1: Float, x2: Float, y2: Float): Pair<Float, Float> {
        val x = x2 + (x2 - x1) * STICK_EXTENSION
        val y = y2 + (y2 - y1) * STICK_EXTENSION
        return Pair(x, y)
    }

    private fun runKalman(rawX: Float, rawY: Float, confidence: Float, isLeft: Boolean): FloatArray {
        val k = if (isLeft) kLeft else kRight

        if (!(if (isLeft) initLeft else initRight)) {
            k[0] = rawX; k[1] = rawY; k[2] = 0f; k[3] = 0f
            if (isLeft) initLeft = true else initRight = true
            return floatArrayOf(rawX, rawY)
        }

        val predX = k[0] + k[2]
        val predY = k[1] + k[3]

        if (confidence > CONFIDENCE_THRESHOLD) {
            val resX = rawX - predX
            val resY = rawY - predY
            k[0] = predX + (KALMAN_ALPHA * resX)
            k[1] = predY + (KALMAN_ALPHA * resY)
            k[2] = k[2] + (KALMAN_BETA * resX)
            k[3] = k[3] + (KALMAN_BETA * resY)
        } else {
            k[0] = predX; k[1] = predY
            k[2] *= 0.8f; k[3] *= 0.8f
        }

        val finalX = k[0] + (k[2] * PREDICTION_FACTOR)
        val finalY = k[1] + (k[3] * PREDICTION_FACTOR)
        return floatArrayOf(finalX.coerceIn(0f, 1f), finalY.coerceIn(0f, 1f))
    }

    private fun getDynamicZone(x: Float, y: Float, chestX: Float, chestY: Float): String {
        val cymbalHeight = chestY - CYMBAL_OFFSET_Y
        val leftDivider = chestX - SNARE_HALF_WIDTH
        val rightDivider = chestX + SNARE_HALF_WIDTH

        if (y < cymbalHeight) {
            return if (x < chestX) "CRASH" else "RIDE"
        } else {
            if (x < leftDivider) return "HI-HAT"
            if (x < rightDivider) return "SNARE"
            return "FLOOR TOM"
        }
    }
}