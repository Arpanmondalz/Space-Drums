package com.example.spacedrums

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Matrix
import android.os.SystemClock
import androidx.camera.core.ImageProxy
import com.google.mediapipe.framework.image.BitmapImageBuilder
import com.google.mediapipe.framework.image.MPImage
import com.google.mediapipe.tasks.core.BaseOptions
import com.google.mediapipe.tasks.core.Delegate
import com.google.mediapipe.tasks.vision.core.RunningMode
import com.google.mediapipe.tasks.vision.poselandmarker.PoseLandmarker
import com.google.mediapipe.tasks.vision.poselandmarker.PoseLandmarkerResult

class PoseDetectorHelper(
    val context: Context,
    val listener: PoseDetectorListener
) {
    private var poseLandmarker: PoseLandmarker? = null

    init {
        setupPoseLandmarker()
    }

    private fun setupPoseLandmarker() {
        val baseOptions = BaseOptions.builder()
            // UPGRADED to full model for better crossed-arm tracking
            .setModelAssetPath("pose_landmarker_full.task")
            .setDelegate(Delegate.GPU)
            .build()

        val options = PoseLandmarker.PoseLandmarkerOptions.builder()
            .setBaseOptions(baseOptions)
            .setMinPoseDetectionConfidence(0.5f)
            .setMinPosePresenceConfidence(0.5f)
            .setMinTrackingConfidence(0.5f)
            .setRunningMode(RunningMode.LIVE_STREAM)
            .setResultListener(this::returnLivestreamResult)
            .setErrorListener(this::returnLivestreamError)
            .build()

        try {
            poseLandmarker = PoseLandmarker.createFromOptions(context, options)
        } catch (e: Exception) {
            listener.onError("PoseLandmarker failed to initialize: ${e.message}")
        }
    }

    fun detectLiveStream(imageProxy: ImageProxy) {
        if (poseLandmarker == null) {
            imageProxy.close()
            return
        }

        try {
            val bitmapBuffer = Bitmap.createBitmap(
                imageProxy.width, imageProxy.height, Bitmap.Config.ARGB_8888
            )
            imageProxy.use { bitmapBuffer.copyPixelsFromBuffer(imageProxy.planes[0].buffer) }

            val matrix = Matrix().apply {
                postRotate(imageProxy.imageInfo.rotationDegrees.toFloat())
                postScale(-1f, 1f, imageProxy.width / 2f, imageProxy.height / 2f)
            }

            val rotatedBitmap = Bitmap.createBitmap(
                bitmapBuffer, 0, 0, bitmapBuffer.width, bitmapBuffer.height, matrix, true
            )

            val mpImage = BitmapImageBuilder(rotatedBitmap).build()
            val frameTime = SystemClock.uptimeMillis()
            poseLandmarker?.detectAsync(mpImage, frameTime)
        } catch (e: Exception) {
            listener.onError(e.message ?: "Frame error")
        }
    }

    private fun returnLivestreamResult(result: PoseLandmarkerResult, input: MPImage) {
        listener.onResults(result)
    }

    private fun returnLivestreamError(error: RuntimeException) {
        listener.onError(error.message ?: "Unknown error")
    }

    interface PoseDetectorListener {
        fun onError(error: String)
        fun onResults(result: PoseLandmarkerResult)
    }
}