package com.ishanp.androidwebcam.companion

/**
 * Outcome of a root-onboarding run. [applied] is decided by the read-back probe
 * after the write — never by the write's exit status — so a ROM that accepts a
 * command and ignores its effect reports "not applied" rather than a false tick.
 *
 * [alreadySatisfied] marks a step that was in place before this ran, so a
 * "2/2" result can distinguish "I fixed both" from "one was already correct".
 */
data class StepResult(
    val id: String,
    val title: String,
    val why: String,
    val applied: Boolean,
    val detail: String,
    val alreadySatisfied: Boolean = false,
)

object RootOnboardingRunner {

    /**
     * Apply every step, verifying each by read-back.
     *
     * Read-before-write: each step's probe runs first, so a fix that is
     * already in place (the user granted the exemption through the system
     * dialog earlier) is reported as already-satisfied and is never written.
     * Only steps that actually need work shell out.
     *
     * [su] is injected so tests never exec a real `su` — on a host that would
     * prompt for a password and block; on the phone it is the actual root call.
     * Steps are independent: a failure in one does not abort the rest, since
     * they target different kill mechanisms and partial application is useful.
     */
    fun applyAll(
        steps: List<RootOnboarding.Step> = RootOnboarding.steps(),
        su: (String) -> Su.Result = Su::run,
    ): List<StepResult> {
        val results = ArrayList<StepResult>(steps.size)
        var rootChecked = false
        var rootAvailable = false
        for (step in steps) {
            val probeCmd = step.probe.firstOrNull()
            val before = probeCmd?.let { su(it) }?.all.orEmpty()
            if (step.appliedWhen(before)) {
                results += StepResult(
                    step.id, step.title, step.why, applied = true,
                    detail = "already in place", alreadySatisfied = true,
                )
                continue
            }
            if (!rootChecked) {
                rootAvailable = su("id -u").let { it.ok && it.out.trim() == "0" }
                rootChecked = true
            }
            if (!rootAvailable) {
                results += StepResult(
                    step.id, step.title, step.why, applied = false,
                    detail = "no root shell (answer the root prompt, then retry)",
                )
                continue
            }
            val write = step.cmds.firstOrNull()
            if (write == null) {
                results += StepResult(step.id, step.title, step.why, false, "no command")
                continue
            }
            val w = su(write)
            // Probe regardless of the write's exit code: some ROMs report
            // failure for a write that landed, and success for one that did not.
            val after = probeCmd?.let { su(it) }?.all.orEmpty()
            val applied = step.appliedWhen(after)
            val detail = when {
                applied -> "verified: ${after.take(120).ifBlank { "(no output)" }}"
                after.isNotBlank() -> "not applied (read-back: ${after.take(120)})"
                w.err.isNotBlank() -> "error: ${w.err.take(120)}"
                else -> "not applied (no read-back output)"
            }
            results += StepResult(step.id, step.title, step.why, applied, detail)
        }
        return results
    }
}
