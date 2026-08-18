# Editable demo captures

This directory holds silent 1920 by 1080 source clips at 30 frames per second. They are separate so
the final narration, captions, pauses and cuts can be changed without re-recording the product.

## Capture set

| File | Contents | Suggested use |
| --- | --- | --- |
| `raw/01-homepage.mp4` | Live landing page and product overview | Opening and product framing |
| `raw/02-agent-guide.mp4` | Machine-readable setup guide | Explain website-driven onboarding |
| `raw/03-codex-setup.mp4` | Real Codex Luna setup in an isolated workspace | Show the agent creating configuration and guidance |
| `raw/04-account-flow.mp4` | Browser application and Logto handoff | Show where sign-in or account creation happens |
| `derived/03-codex-setup-fast.mp4` | Clean opening followed by accelerated agent work | Short edits where the complete run is too long |
| `rough-cut.mp4` | Silent assembly of the four scenes | Starting point for the editor |
| `full/raw/05-live-agent-workflow.mp4` | Real OAuth identity, write, queue drain and find | Long-form agent evidence |
| `full/raw/05b-grounded-examples.mp4` | Two more grounded questions against the new source | Find variation evidence |
| `full/raw/06-authenticated-console.mp4` | Authenticated sources, findings, subjects and themes | Product console evidence |
| `full/raw/07-aws-architecture.mp4` | Public AWS architecture and deployment guide | Infrastructure explanation |
| `full/raw/08-cloud-evidence.mp4` | Live endpoint, ccloud, C-SPANN and Lambda evidence | Cloud proof |
| `full/craizk-complete-demo.mp4` | Silent long-form assembly of the complete flow | Main source for the final narrated edit |

The terminal take stops before authentication. It mounts only a disposable workspace and the
existing Codex authentication file. The exported picture contains no token, email address, AWS
account number or private application data.

The accelerated terminal derivative skips the standard Codex startup metadata. The complete raw
take remains available for alternate edits.

[EDIT_PLAN.md](EDIT_PLAN.md) records the short and long timelines. The complete assembly is about
nine minutes and ten seconds. It keeps the successful live flow at normal speed so the final
hackathon edit can choose its own pauses and evidence cuts.

## Rebuild

Run each capture with a hard outer timeout from the repository root.

```sh
timeout --signal=TERM --kill-after=10s 180s bash hackathon/video/capture-browser.sh
timeout --signal=TERM --kill-after=10s 240s bash hackathon/video/capture-terminal.sh
bash hackathon/video/montage.sh
timeout --signal=TERM --kill-after=10s 420s bash hackathon/video/capture-full-agent.sh
timeout --signal=TERM --kill-after=10s 180s bash hackathon/video/capture-full-agent-examples.sh
timeout --signal=TERM --kill-after=10s 300s bash hackathon/video/capture-full-browser.sh
timeout --signal=TERM --kill-after=10s 180s bash hackathon/video/capture-full-ops.sh
bash hackathon/video/full-montage.sh
```

Record narration separately. Keep the raw clips unchanged and make editorial cuts from copies.
