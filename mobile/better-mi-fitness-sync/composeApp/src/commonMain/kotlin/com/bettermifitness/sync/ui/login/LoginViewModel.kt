package com.bettermifitness.sync.ui.login

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.bettermifitness.sync.data.MiRegionDiscovery
import com.bettermifitness.sync.data.MiSessionManager
import com.bettermifitness.sync.data.preferences.CredentialsStore
import com.bettermifitness.sync.i18n.L10n
import com.mifitness.miclient.auth.LoginResult
import com.mifitness.miclient.auth.MiAuth
import com.mifitness.miclient.auth.MiCredentials
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

enum class LoginStep {
    Credentials,
    Otp,
    Captcha,
    BrowserFallback,
}

data class LoginUiState(
    val step: LoginStep = LoginStep.Credentials,
    val email: String = "",
    val password: String = "",
    val isLoading: Boolean = false,
    val errorMessage: String? = null,
    val otpMaskedTarget: String = "",
    val loginSucceeded: Boolean = false,
    val browserLoginUrl: String = "",
    val captchaImage: ByteArray? = null,
    val captchaIck: String = "",
    val captchaLoading: Boolean = false,
)

class LoginViewModel(
    private val miAuth: MiAuth,
    private val sessionManager: MiSessionManager,
    private val credentialsStore: CredentialsStore,
    private val regionDiscovery: MiRegionDiscovery,
) : ViewModel() {

    val uiState: StateFlow<LoginUiState>
        field = MutableStateFlow(LoginUiState())

    private var otpChallenge: LoginResult.OtpRequired? = null
    private var captchaChallenge: LoginResult.CaptchaRequired? = null

    /**
     * When true, Back on browser returns to OTP (user chose browser from OTP).
     * When false, Back returns to credentials (OTP was skipped — e.g. send rate-limited).
     */
    private var browserBackGoesToOtp: Boolean = false

    fun onEmailChange(value: String) {
        uiState.update { it.copy(email = value) }
    }

    fun onPasswordChange(value: String) {
        uiState.update { it.copy(password = value) }
    }

    fun signIn() {
        val email = uiState.value.email.trim()
        val password = uiState.value.password
        if (email.isBlank() || password.isBlank()) return

        viewModelScope.launch {
            uiState.update { it.copy(isLoading = true, errorMessage = null) }
            try {
                val deviceId = credentialsStore.ensureDeviceId()
                when (val result = miAuth.login(email = email, password = password, deviceId = deviceId)) {
                    is LoginResult.Success -> persistAndSucceed(result.credentials)
                    is LoginResult.CaptchaRequired -> {
                        otpChallenge = null
                        enterCaptchaChallenge(result, errorMessage = null)
                    }
                    is LoginResult.OtpRequired -> {
                        otpChallenge = result
                        captchaChallenge = null
                        // No email OTP exists when notificationUrl is blank (captcha
                        // or 2FA gate): with no OTP to send, browser is the only path.
                        // A real OTP challenge always carries notificationUrl.
                        if (result.notificationUrl.isBlank()) {
                            browserBackGoesToOtp = false
                            otpChallenge = null
                            captchaChallenge = null
                            val url = browserLoginUrl()
                            uiState.update {
                                it.copy(
                                    isLoading = false,
                                    step = LoginStep.BrowserFallback,
                                    otpMaskedTarget = "",
                                    browserLoginUrl = url,
                                    errorMessage = L10n.text(L10n.loginBrowserRequired),
                                )
                            }
                            return@launch
                        }
                        try {
                            result.sendOtp()
                            browserBackGoesToOtp = false
                            uiState.update {
                                it.copy(
                                    isLoading = false,
                                    step = LoginStep.Otp,
                                    otpMaskedTarget = result.maskedTarget,
                                    errorMessage = null,
                                )
                            }
                        } catch (e: Exception) {
                            // OTP email send failed (rate limit): browser bypasses
                            // OTP with the trusted id, so it is the correct fallback.
                            browserBackGoesToOtp = false
                            otpChallenge = null
                            captchaChallenge = null
                            val url = browserLoginUrl()
                            uiState.update {
                                it.copy(
                                    isLoading = false,
                                    step = LoginStep.BrowserFallback,
                                    otpMaskedTarget = "",
                                    browserLoginUrl = url,
                                    errorMessage = e.message ?: L10n.text(L10n.loginSendCodeFailed),
                                )
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                uiState.update {
                    it.copy(isLoading = false, errorMessage = e.message ?: L10n.text(L10n.loginFailed))
                }
            }
        }
    }

    fun verifyOtp(code: String) {
        val challenge = otpChallenge ?: return
        viewModelScope.launch {
            uiState.update { it.copy(isLoading = true, errorMessage = null) }
            try {
                val credentials = challenge.verifyOtp(code)
                persistAndSucceed(credentials)
            } catch (e: Exception) {
                val msg = e.message ?: L10n.text(L10n.loginVerificationFailed)
                if (shouldFallbackToBrowser(msg)) {
                    browserBackGoesToOtp = true
                    val url = browserLoginUrl()
                    uiState.update {
                        it.copy(
                            isLoading = false,
                            step = LoginStep.BrowserFallback,
                            browserLoginUrl = url,
                            errorMessage =
                                L10n.text(L10n.loginBrowserRequired),
                        )
                    }
                } else {
                    uiState.update { it.copy(isLoading = false, errorMessage = msg) }
                }
            }
        }
    }

    fun resendOtp() {
        viewModelScope.launch {
            uiState.update { it.copy(errorMessage = null) }
            try {
                otpChallenge?.sendOtp()
                uiState.update { it.copy(errorMessage = L10n.text(L10n.loginCodeResent)) }
            } catch (e: Exception) {
                uiState.update {
                    it.copy(errorMessage = L10n.textFmt(L10n.loginResendFailed, e.message ?: ""))
                }
            }
        }
    }

    /** Parks a picture challenge and loads its first image for the Captcha step. */
    private fun enterCaptchaChallenge(
        challenge: LoginResult.CaptchaRequired,
        errorMessage: String?,
    ) {
        captchaChallenge = challenge
        viewModelScope.launch {
            uiState.update {
                it.copy(
                    isLoading = false,
                    step = LoginStep.Captcha,
                    // Keep the current image until the fresh one arrives: the
                    // code the user types always belongs to the shown picture.
                    captchaLoading = true,
                    errorMessage = errorMessage,
                )
            }
            try {
                val image = challenge.fetchImage()
                uiState.update {
                    it.copy(captchaImage = image.bytes, captchaIck = image.ick, captchaLoading = false)
                }
            } catch (e: Exception) {
                uiState.update {
                    it.copy(
                        captchaLoading = false,
                        errorMessage = e.message ?: L10n.text(L10n.loginCaptchaLoadFailed),
                    )
                }
            }
        }
    }

    fun refreshCaptcha() {
        val challenge = captchaChallenge ?: return
        if (uiState.value.captchaLoading) return
        viewModelScope.launch {
            uiState.update { it.copy(captchaLoading = true, errorMessage = null) }
            try {
                val image = challenge.fetchImage()
                uiState.update {
                    it.copy(captchaImage = image.bytes, captchaIck = image.ick, captchaLoading = false)
                }
            } catch (e: Exception) {
                uiState.update {
                    it.copy(
                        captchaLoading = false,
                        errorMessage = e.message ?: L10n.text(L10n.loginCaptchaLoadFailed),
                    )
                }
            }
        }
    }

    fun submitCaptcha(code: String) {
        val challenge = captchaChallenge ?: return
        val trimmed = code.trim()
        if (trimmed.isEmpty()) {
            uiState.update { it.copy(errorMessage = L10n.text(L10n.loginCaptchaEmpty)) }
            return
        }
        val ick = uiState.value.captchaIck
        if (ick.isBlank()) {
            uiState.update { it.copy(errorMessage = L10n.text(L10n.loginCaptchaLoadFailed)) }
            return
        }
        viewModelScope.launch {
            uiState.update { it.copy(isLoading = true, errorMessage = null) }
            try {
                when (val result = challenge.submitCode(trimmed, ick)) {
                    is LoginResult.Success -> persistAndSucceed(result.credentials)
                    is LoginResult.CaptchaRequired -> {
                        // Wrong code: park the fresh challenge and load a new picture.
                        // Do NOT auto-route here — the user may have escaped to the
                        // browser meanwhile; enterCaptchaChallenge owns that choice.
                        enterCaptchaChallenge(
                            result,
                            errorMessage = L10n.text(L10n.loginCaptchaWrong),
                        )
                    }
                    is LoginResult.OtpRequired -> {
                        captchaChallenge = null
                        otpChallenge = result
                        browserBackGoesToOtp = false
                        if (result.notificationUrl.isBlank()) {
                            val url = browserLoginUrl()
                            uiState.update {
                                it.copy(
                                    isLoading = false,
                                    step = LoginStep.BrowserFallback,
                                    otpMaskedTarget = "",
                                    browserLoginUrl = url,
                                    errorMessage = L10n.text(L10n.loginBrowserRequired),
                                )
                            }
                        } else {
                            try {
                                result.sendOtp()
                                uiState.update {
                                    it.copy(
                                        isLoading = false,
                                        step = LoginStep.Otp,
                                        otpMaskedTarget = result.maskedTarget,
                                        errorMessage = null,
                                    )
                                }
                            } catch (e: Exception) {
                                val url = browserLoginUrl()
                                uiState.update {
                                    it.copy(
                                        isLoading = false,
                                        step = LoginStep.BrowserFallback,
                                        otpMaskedTarget = "",
                                        browserLoginUrl = url,
                                        errorMessage = e.message ?: L10n.text(L10n.loginSendCodeFailed),
                                    )
                                }
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                uiState.update {
                    it.copy(isLoading = false, errorMessage = e.message ?: L10n.text(L10n.loginFailed))
                }
            }
        }
    }

    fun goBackFromCaptcha() {
        captchaChallenge = null
        uiState.update {
            it.copy(
                step = LoginStep.Credentials,
                errorMessage = null,
                captchaImage = null,
                captchaIck = "",
                captchaLoading = false,
            )
        }
    }

    fun completeBrowserLogin(callbackUrl: String) {
        val cleaned = callbackUrl.trim()
        if (cleaned.isBlank()) {
            uiState.update {
                it.copy(errorMessage = "Paste the full redirect URL from the address bar first.")
            }
            return
        }
        // Browser login is query-only: the pasted URL carries the browser-trusted
        // device id (d=) and nothing else usable. Adopt that id, then retry
        // email+password on it so OTP is bypassed and captcha (if any) is solved
        // in the captcha step. Never expect a session from the pasted URL.
        val email = uiState.value.email.trim()
        val password = uiState.value.password
        val trustedDeviceId = extractDeviceId(cleaned)

        viewModelScope.launch {
            uiState.update { it.copy(isLoading = true, errorMessage = null) }
            if (trustedDeviceId.isBlank()) {
                uiState.update {
                    it.copy(
                        isLoading = false,
                        errorMessage = "That URL has no device id (d=…). Copy the full address bar URL.",
                    )
                }
                return@launch
            }
            credentialsStore.restoreDeviceId(trustedDeviceId)
            if (email.isBlank() || password.isBlank()) {
                uiState.update {
                    it.copy(
                        isLoading = false,
                        errorMessage = "Enter your email and password first, then paste the browser URL.",
                    )
                }
                return@launch
            }
            try {
                when (val result = miAuth.login(email = email, password = password, deviceId = trustedDeviceId)) {
                    is LoginResult.Success -> persistAndSucceed(result.credentials)
                    is LoginResult.OtpRequired -> {
                        // OTP send rate-limited on the trusted id: park the
                        // challenge and stay on browser — only OTP rate limit
                        // justifies the browser path.
                        otpChallenge = result
                        browserBackGoesToOtp = false
                        val url = browserLoginUrl()
                        uiState.update {
                            it.copy(
                                isLoading = false,
                                step = LoginStep.BrowserFallback,
                                otpMaskedTarget = result.maskedTarget,
                                browserLoginUrl = url,
                                errorMessage = result.notificationUrl.ifBlank {
                                    L10n.text(L10n.loginBrowserRequired)
                                },
                            )
                        }
                    }
                    is LoginResult.CaptchaRequired -> {
                        // Captcha on the trusted id: solve it there with the typed
                        // code — never bounce back to browser, which cannot help.
                        otpChallenge = null
                        enterCaptchaChallenge(result, errorMessage = null)
                    }
                }
                return@launch
            } catch (e: Exception) {
                uiState.update {
                    it.copy(
                        isLoading = false,
                        errorMessage = e.message
                            ?: "Could not finish browser login from that URL. Paste the full sts-hlth URL.",
                    )
                }
            }
        }
    }

    fun goToBrowserFallback() {
        // User left OTP intentionally (“Having trouble?”) — Back should restore OTP.
        browserBackGoesToOtp = otpChallenge != null
        viewModelScope.launch {
            val url = browserLoginUrl()
            uiState.update {
                it.copy(step = LoginStep.BrowserFallback, errorMessage = null, browserLoginUrl = url)
            }
        }
    }
    /** Resolves the per-install browser URL for the UI layer (blank until loaded). */
    fun openBrowserLogin(onUrl: (String) -> Unit) {
        viewModelScope.launch {
            onUrl(browserLoginUrl())
        }
    }

    /**
     * Browser login URL bound to this install's stable device identity, so the trust
     * granted in the browser lands on the device id the app refreshes with.
     */
    suspend fun browserLoginUrl(): String {
        val deviceId = credentialsStore.ensureDeviceId()
        return miAuth.buildLoginUrl(deviceId = deviceId)
    }

    /**
     * Back from browser login:
     * - OTP if the user opened browser from the OTP step
     * - Credentials if OTP was skipped (e.g. email send rate-limited)
     */
    fun goBackFromBrowser() {
        if (browserBackGoesToOtp && otpChallenge != null) {
            uiState.update {
                it.copy(step = LoginStep.Otp, errorMessage = null)
            }
            return
        }
        browserBackGoesToOtp = false
        otpChallenge = null
        uiState.update {
            it.copy(
                step = LoginStep.Credentials,
                errorMessage = null,
                otpMaskedTarget = "",
                browserLoginUrl = "",
            )
        }
    }

    fun goBackToCredentials() {
        otpChallenge = null
        captchaChallenge = null
        browserBackGoesToOtp = false
        uiState.update {
            it.copy(
                step = LoginStep.Credentials,
                errorMessage = null,
                otpMaskedTarget = "",
                browserLoginUrl = "",
                captchaImage = null,
                captchaIck = "",
                captchaLoading = false,
            )
        }
    }

    fun consumeLoginSuccess() {
        uiState.update { it.copy(loginSucceeded = false) }
    }

    private suspend fun persistAndSucceed(credentials: MiCredentials) {
        // Trust now lives on the browser-adopted id: persist what login returned
        // instead of normalizing back to a previous install id.
        val effective = credentials
        if (effective.passToken.isBlank()) {
            uiState.update {
                it.copy(
                    isLoading = false,
                    errorMessage = "Login did not return a passToken, so the app cannot stay " +
                        "signed in across days. Try again or use the other login method.",
                )
            }
            return
        }
        if (effective.deviceId.isBlank()) {
            uiState.update {
                it.copy(
                    isLoading = false,
                    errorMessage = "Login missing device id — try again.",
                )
            }
            return
        }
        sessionManager.activate(effective)
        credentialsStore.saveCredentials(effective)
        // Pick the health cloud shard that actually holds the newest samples.
        try {
            val discovered = regionDiscovery.discover(effective)
            credentialsStore.setDiscoveredRegion(discovered)
            sessionManager.reloadFromStore()
        } catch (_: Exception) {
            // Keep STS provisional region if probing fails entirely.
        }
        uiState.update {
            it.copy(
                isLoading = false,
                loginSucceeded = true,
                errorMessage = null,
                // Drop OTP/browser step so Back cannot reopen them after success.
                step = LoginStep.Credentials,
                browserLoginUrl = "",
            )
        }
    }

    companion object {
        private val OTP_BROWSER_HINTS = listOf(
            "OTP_ACCEPTED_NEEDS_BROWSER",
            "still require",
            "still won't",
            "browser login",
        )

        fun shouldFallbackToBrowser(message: String): Boolean =
            OTP_BROWSER_HINTS.any { message.contains(it, ignoreCase = true) }

        /** Shared routing so Compose, iOS, and tests agree on picture vs browser. */
        fun isPictureCaptchaForStep(type: String): Boolean =
            com.mifitness.miclient.auth.PassportAuthUtils.isPictureCaptchaType(type)

        // Browser-trusted device id from the pasted STS redirect URL (d=/deviceId).
        // Adopted as this install's id so the password retry bypasses OTP.
        fun extractDeviceId(url: String): String {
            val cleaned = url.trim().lines().firstOrNull { it.isNotBlank() }?.trim() ?: return ""
            // Xiaomi uses d= on STS; some pages also use deviceId=
            val patterns = listOf(
                Regex("[?&]d=([^&]+)"),
                Regex("[?&]deviceId=([^&]+)"),
                Regex("[?&]device_id=([^&]+)"),
            )
            for (regex in patterns) {
                val value = regex.find(cleaned)?.groupValues?.get(1)
                if (!value.isNullOrBlank()) return value
            }
            return ""
        }
    }
}
