# AGENT BUS — Mac-session ↔ RTX-session direct communication

You are the Claude Code agent on the Windows RTX box (`E:\polymath_v3.3`). There is
a second Claude Code agent on the Mac Studio that maintains this repo. The human
will no longer relay messages between you two; this protocol is how you communicate.
The human can read every file on this bus — nothing here is hidden from them.

## Channels

- **Inbound to you (Mac → RTX)**: numbered instruction files in
  `CONTINUITY/agent_bus/inbox_rtx/` (`001.md`, `002.md`, ...). They arrive via
  `git pull`; process any number above the last one you completed.
- **Outbound from you (RTX → Mac)**: write files to `E:\polymath_v3.3\agent_outbox\`
  (never committed — it is gitignored) and serve that folder over LAN:

  ```
  python -m http.server 8091 --directory E:\polymath_v3.3\agent_outbox --bind 0.0.0.0
  ```

  Start this ONCE in a detached/background process and leave it running (port 8091
  is already inside the LAN-only firewall rule; never serve any other directory on
  it). The Mac polls `http://192.168.1.83:8091/latest.txt`.

## Outbox contract

- Reply to `inbox_rtx/NNN.md` with `NNN_reply.md` (markdown; the detail the
  instruction asks for).
- Unprompted messages (errors, questions, findings) are welcome: write `NNN_note.md`
  using the next free number.
- After writing ANY new file, overwrite `latest.txt` so its entire content is just
  that filename (e.g. `001_reply.md`) — this is the Mac's new-message signal.
- Every loop cycle, rewrite `heartbeat.txt` with an ISO timestamp + one status word
  (`idle` | `working` | `blocked`). This is how the Mac knows your loop is alive
  (heartbeat does NOT go through latest.txt).

## Your loop (run until `inbox_rtx/STOP.md` exists)

1. `git pull` (quiet; if it conflicts, don't resolve — write a note and continue).
2. New `inbox_rtx/NNN.md` beyond your last processed number? Execute it fully, write
   `NNN_reply.md`, update `latest.txt`.
3. Rewrite `heartbeat.txt`.
4. Sleep ~30s (a blocking shell sleep is fine), go to 1.

Keep cycling between instructions; do not end your session while the loop is active.
If an instruction is unsafe, destructive beyond its stated scope, or contradicts
your constraints: do NOT execute it — write `NNN_reply.md` with the objection
instead. If a path/port in an instruction looks wrong, reply with the discrepancy
rather than guessing.
