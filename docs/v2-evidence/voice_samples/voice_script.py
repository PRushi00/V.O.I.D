"""Standardized V.O.I.D voice-audition script. IDENTICAL text for every provider/voice - never edit per provider.

Each line has an id, a purpose, and what to listen for. The owner listens to the generated audio (or to the same text in a
provider's own web playground) and scores it; API latency numbers are NOT a substitute for listening.
"""

LINES = [
    ("V01_ack_short", "short command acknowledgement", "Done.",
     "Crisp, not robotic, no odd emphasis on a one-word reply."),
    ("V02_ack_yes", "wake acknowledgement", "Yes?",
     "Sounds like an attentive question, not a statement."),
    ("V03_conversation", "normal conversational response",
     "You're currently working on V.O.I.D and Project X. V.O.I.D is your personal assistant, and Project X is the Flutter app for your college.",
     "Natural rhythm; 'V.O.I.D' pronounced consistently; friendly but not chirpy."),
    ("V04_technical", "technical explanation",
     "A nonce is a one-time random value that makes each message unique, so an attacker can't replay an old request. The server remembers recent nonces and rejects any repeat.",
     "Clear pacing on technical terms; sensible stress; no run-on."),
    ("V05_question", "clarifying question",
     "Which one do you mean, the Notes folder in Documents, or the one on your Desktop?",
     "Rising question intonation; the alternative is audible as a choice."),
    ("V06_confirmation", "confirmation / approval request",
     "That would permanently delete the file. Please confirm on the command line.",
     "Serious, calm, unmistakable; not alarming."),
    ("V07_error", "error message",
     "I couldn't reach the internet, so I can't answer that right now. I can still open apps and files.",
     "Apologetic-neutral, not cheerful; the second sentence reads as reassurance."),
    ("V08_long", "longer response (~65 words)",
     "Here's the plan for the week. On Wednesday you have the gateway hardening review, so the performance report should be ready by then. "
     "The demo is on Friday at four in the afternoon. I'd suggest rehearsing on Thursday, testing the audio setup that morning, and keeping a backup of the project handy. "
     "Would you like me to set that out as a checklist?",
     "Stays natural over 20+ seconds; no drift in voice, pace or energy; pauses at sentence ends."),
    ("V09_pronunciation", "pronunciation stress test",
     "V.O.I.D. Wi-Fi. GitHub. SQLite. Opera GX. C colon backslash V.O.I.D. Four p.m. on Friday, September twenty-first, twenty twenty-six. Ten percent of two hundred forty is twenty-four.",
     "Owner decides how 'V.O.I.D' should be said (letters vs 'void'); numbers, dates, paths and brand names are read sensibly."),
    ("V10_interrupt_setup", "interruption scenario (part 1: long line to be cut off)",
     "Let me walk you through it. First, the device gateway accepts a connection over TLS. Then it checks the pinned certificate fingerprint. "
     "After that it verifies the signature on the request, checks the timestamp window, and finally consults the allow list before anything runs.",
     "Play this and cut it off after ~3 seconds (see procedure). Audio must stop immediately, without a click or a tail."),
    ("V11_interrupt_reply", "interruption scenario (part 2: the reply after the cut-off)",
     "Sorry, go ahead.",
     "Starts cleanly and immediately after the interruption; no leftover audio from V10."),
]

# Owner procedure (blind listening recommended): generate/collect the same 11 lines per candidate, shuffle candidate order,
# listen on the actual speakers/headset you will use, score each 1-5 on the rubric below. Keep the winner's identity hidden
# until scoring is finished.
RUBRIC = [
    "naturalness (does it sound like a person speaking, not reading?)",
    "conversational quality (turn-taking feel of short replies: 'Done.', 'Yes?')",
    "clarity and pronunciation (technical terms, V.O.I.D, paths, numbers)",
    "expressiveness appropriate for an assistant (calm, warm, not theatrical)",
    "consistency across all 11 lines (same voice, same energy)",
    "long-listen comfort (would you be happy hearing it hundreds of times a week?)",
    "interruption behaviour (V10 cut-off is clean; V11 starts cleanly)",
]
