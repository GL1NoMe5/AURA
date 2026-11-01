<div align="center">

<a href="https://gl1nome5.github.io/AURA/">
  <img src="docs/readme/hero.png" width="100%" alt="AURA. Application recovery. A portrait made of light.">
</a>

[Watch the film](https://gl1nome5.github.io/AURA/) &nbsp; · &nbsp; [Measured results](#measured-on-linkding) &nbsp; · &nbsp; [Run locally](#run-locally)

</div>

<br>

AURA helps small teams look after Python applications running in Docker.
When an app stops responding, Meta AI agents inspect its logs, resource use and
settings. AURA can restore a saved configuration within permissions you set,
then check that real requests work again.

For a supported code defect, it can prepare a patch and test it in an isolated
container. You review the change and approve the specific version before deployment.

## Measured on Linkding

A changed port left Linkding running but unreachable. AURA detected the failed
requests, compared the current settings with the saved configuration, and restored
the working port automatically.

![Linkding configuration recovery: port 9091 restored to 9090; availability verified in 93.301 seconds, using three model calls and 18 complete observations.](docs/readme/recovery.png)

This was one recorded run on a disposable Docker instance. Automatic restoration
was enabled beforehand; no person intervened after the fault.
[Run details and measurements](docs/LIVE_RECOVERY.md).

<details>
<summary>A separate code repair: large files download again</summary>

<br>

A historical Linkding defect buffered an entire file in memory. Under a 256 MiB
limit, neither of two 500 MiB downloads completed. The AI proposal needed a human
correction before AURA tested and deployed the approved patch.

![Both 500 MiB downloads completed with matching hashes after the reviewed fix. Seven post-deployment memory samples stayed below the 256 MiB limit.](docs/presentation/media/verified-result.png)

Both routes returned the full file with matching hashes and no new OOM kills.
The highest of seven memory samples was 175.8 MiB.
[Case study](docs/presentation/CASE_STUDY.md) · [Chart data](docs/presentation/case-data.json).

</details>

## Inside AURA

![Docker observations feed relevant Meta Open 1026 specialists. A diagnosis can lead to policy-authorized recovery, or a code patch that is tested and approved before deployment. Both paths end with checks against the running application.](docs/readme/architecture.gif)

All runtime model calls use **Meta Muse Spark Open 1026 through Baseten**.
The controller chooses the relevant specialists and limits each investigation.
The animation illustrates the flow; it is not a recording of model activity.

<details>
<summary>Stack and operating limits</summary>

<br>

| Part | Implementation |
| --- | --- |
| Interface | React, TypeScript, Three.js, custom GLSL particles |
| Controller | Python, FastAPI, Docker Engine |
| Model | `meta-models/muse-spark-open-1026`, served by Baseten |
| History and progress | SQLite, WebSocket, checksummed artifacts |
| Code checks | Isolated container, reproducing test before and after the change, existing application tests |

This local prototype supports up to three Docker applications and one active
investigation. Linkding is the repair case; Healthchecks was used to check
connection, observation and restart on a second application. Remote server fleets
and unattended production use have not been validated.

New connections start in observation mode. Recovery permissions are opt-in.
Code repair needs a configured source and test profile; a tested patch still
requires approval. Stopping AURA leaves connected applications running.

[Connection and repair profiles](docs/TARGET_MANAGEMENT.md) ·
[API](docs/OPS_API.md) · [Verification record](docs/FINAL_ACCEPTANCE.md) ·
[Still diagram](docs/readme/architecture.png)

</details>

## Run locally

Windows 10/11, Python 3.10+, [uv](https://docs.astral.sh/uv/getting-started/installation/),
Node.js 22.12+ and Docker Desktop with Linux containers.

```powershell
git clone https://github.com/GL1NoMe5/AURA.git
cd AURA
.\Start-AURA.cmd --setup
.\Connect-Baseten.cmd
.\Start-AURA.cmd
```

Open **[localhost:8000](http://127.0.0.1:8000/)**, choose **My apps**, then
**Connect an app**. The **?** button opens the guided tour.

You can skip `Connect-Baseten.cmd` to explore the interface without model calls.
Investigations require a compatible Baseten key and use provider credits.
The empty app starts without Docker; connecting containers requires it.
[Setup and troubleshooting](docs/CLEAN_START.md).

<br>

<div align="center">

Built for the Meta Global AI Developer Hackathon<br>
<sub>AURA and portrait artwork by Emir · <a href="LICENSE">MIT code</a> · <a href="MEDIA_LICENSE.md">Media terms</a> · <a href="THIRD_PARTY_NOTICES.md">Credits</a></sub>

</div>
