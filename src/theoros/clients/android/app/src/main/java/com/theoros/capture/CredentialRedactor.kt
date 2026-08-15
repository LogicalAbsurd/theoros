package com.theoros.capture

/**
 * Credential pattern redaction for captured text.
 *
 * Mirrors src/theoros/services/capture/redact.py on the desktop pipeline.
 * Scans content for known secret patterns and replaces matches with
 * [REDACTED:<pattern_name>] before text is logged or transmitted.
 *
 * Pattern order matters — more specific patterns run before less specific
 * ones (Anthropic keys before OpenAI keys, etc.).
 */
object CredentialRedactor {

    /**
     * Result of a redaction pass.
     *
     * @property text The redacted text.
     * @property appliedPatterns Names of patterns that matched at least once,
     *                            in the order they were checked.
     */
    data class Result(val text: String, val appliedPatterns: List<String>)

    private data class Pattern(val name: String, val regex: Regex)

    // Patterns are checked in order. Specific before general.
    private val patterns = listOf(
        Pattern("anthropic_api_key", Regex("""sk-ant-[a-zA-Z0-9_-]{20,}""")),
        Pattern("openai_api_key",    Regex("""sk-(?!ant-)(?:proj-)?[a-zA-Z0-9_-]{20,}""")),
        Pattern("aws_access_key",    Regex("""\bAKIA[A-Z0-9]{16}\b""")),
        Pattern("stripe_key",        Regex("""\b(?:sk|pk|rk)_(?:live|test)_[a-zA-Z0-9]{24,}\b""")),
        Pattern("jwt_token",         Regex("""\beyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b""")),
        Pattern("credit_card",       Regex("""\b(?:\d[ -]?){13,19}\b""")),
        Pattern("us_ssn",            Regex("""\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b""")),
    )

    /**
     * Scan text and redact any matches in place.
     *
     * Credit-card matches require Luhn validation before redaction — a digit
     * sequence of plausible length is not enough.
     */
    fun redact(input: String): Result {
        var text = input
        val applied = mutableListOf<String>()

        for (pattern in patterns) {
            if (pattern.name == "credit_card") {
                var matched = false
                text = pattern.regex.replace(text) { match ->
                    val digits = match.value.replace(Regex("[ -]"), "")
                    if (digits.length < 13 || digits.length > 19) {
                        match.value
                    } else if (!luhnCheck(digits)) {
                        match.value
                    } else {
                        matched = true
                        "[REDACTED:credit_card]"
                    }
                }
                if (matched) applied.add(pattern.name)
            } else {
                val newText = pattern.regex.replace(text, "[REDACTED:${pattern.name}]")
                if (newText != text) applied.add(pattern.name)
                text = newText
            }
        }

        return Result(text, applied)
    }

    /** Luhn checksum validation for credit-card digit strings. */
    private fun luhnCheck(digits: String): Boolean {
        var total = 0
        for ((i, ch) in digits.reversed().withIndex()) {
            var d = ch.digitToInt()
            if (i % 2 == 1) {
                d *= 2
                if (d > 9) d -= 9
            }
            total += d
        }
        return total % 10 == 0
    }
}