<div align="center">

<a href="https://gl1nome5.github.io/AURA/">
  <img src="docs/readme/hero.png" width="100%" alt="AURA. Application recovery. A portrait made of light.">
</a>

<br>
<br>

When a Python app in Docker stops responding, AURA works out why<br>
and checks the recovery against real requests.

<br>

<img alt="Meta Open 1026 on Baseten" src="https://img.shields.io/badge/Meta%20Open%201026-Baseten-17343e?style=flat-square&labelColor=0b1f2a&logo=meta&logoColor=94d5c8">
<img alt="Python and FastAPI" src="https://img.shields.io/badge/Python-FastAPI-17343e?style=flat-square&labelColor=0b1f2a&logo=python&logoColor=94d5c8">
<img alt="React and Three.js" src="https://img.shields.io/badge/React-Three.js-17343e?style=flat-square&labelColor=0b1f2a&logo=react&logoColor=94d5c8">
<img alt="Docker" src="https://img.shields.io/badge/Docker-Linux%20containers-17343e?style=flat-square&labelColor=0b1f2a&logo=docker&logoColor=94d5c8">
<a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/code-MIT-17343e?style=flat-square&labelColor=0b1f2a"></a>

<br>
<br>

[Watch the film](https://gl1nome5.github.io/AURA/) &nbsp;·&nbsp;
[How it works](#how-it-works) &nbsp;·&nbsp;
[Results](#measured-on-linkding) &nbsp;·&nbsp;
[Guardrails](#guardrails) &nbsp;·&nbsp;
[Run locally](#run-locally)

</div>

<br>

AURA helps small teams look after Python applications running in Docker.
When an app stops responding, Meta AI agents inspect its logs, resource use and
settings. AURA can restore a saved configuration within permissions you set,
then check that real requests work again.

For a supported code defect, it can prepare a patch and test it in an isolated
container. You review the change and approve the specific version before deployment.

## How it works

<table>
<tr>
<td width="25%" valign="top">
<sub>01</sub><br>
<b>Observe</b>
<p>Health checks, logs, memory and settings for each connected container.</p>
</td>
<td width="25%" valign="top">
<sub>02</sub><br>
<b>Investigate</b>
<p>Meta Open 1026 specialists read the evidence after secrets are redacted locally.</p>
</td>
<td width="25%" valign="top">
<sub>03</sub><br>
<b>Act</b>
<p>Restore saved settings if your policy allows it, or prepare a code change for testing and approval.</p>
</td>
<td width="25%" valign="top">
<sub>04</sub><br>
<b>Verify</b>
<p>Requests to the running app decide whether the incident is resolved.</p>
</td>
</tr>
</table>

![Docker observations feed relevant Meta Open 1026 specialists. A diagnosis can lead to policy-authorized recovery, or a code patch that is tested and approved before deployment. Both paths end with checks against the running application.](docs/readme/architecture.gif)

<sub>All runtime model calls use Meta Muse Spark Open 1026 through Baseten. The controller chooses the relevant specialists and limits each investigation. The animation illustrates the flow; it is not a recording of model activity.</sub>

## Measured on Linkding

A changed port left Linkding running but unreachable. AURA detected the failed
requests, compared the current settings with the saved configuration, and restored
the working port automatically.

![Linkding configuration recovery: port 9091 restored to 9090; availability verified in 93.301 seconds, using three model calls and 18 complete observations.](docs/readme/recovery.png)

<sub>One recorded run on a disposable Docker instance. Automatic restoration was enabled beforehand; no person intervened after the fault. <a href="docs/LIVE_RECOVERY.md">Run details and measurements</a></sub>

<br>

### A separate code repair

A historical Linkding defect buffered an entire file in memory. Under a 256 MiB
limit, neither of two 500 MiB downloads completed. The AI proposal needed a human
correction before AURA tested and deployed the approved patch.

![Both 500 MiB downloads completed with matching hashes after the reviewed fix. Seven post-deployment memory samples stayed below the 256 MiB limit.](docs/presentation/media/verified-result.png)

<sub>Both routes returned the full file with matching hashes and no new OOM kills. The highest of seven memory samples was 175.8 MiB. <a href="docs/presentation/CASE_STUDY.md">Case study</a> · <a href="docs/presentation/case-data.json">Chart data</a></sub>

## Guardrails

<table>
<tr>
<td width="50%" valign="top">
<b>Observation first</b>
<p>New connections only watch. Each recovery permission is opt-in for that application.</p>
</td>
<td width="50%" valign="top">
<b>Sealed test container</b>
<p>Candidate patches run with no network, all Linux capabilities dropped and privilege escalation blocked.</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<b>Evidence on both sides of a fix</b>
<p>A reproducing test must fail on the old code and pass on the new code. The application's existing tests must still pass.</p>
</td>
<td width="50%" valign="top">
<b>Approval for one version</b>
<p>You approve the specific tested patch. A different change needs its own approval.</p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<b>Checked after deployment</b>
<p>AURA repeats the real operation against the running app and can roll back the container it deployed.</p>
</td>
<td width="50%" valign="top">
<b>Your apps keep running</b>
<p>Stopping AURA leaves connected applications running.</p>
</td>
</tr>
</table>

## Under the hood

| Part | Implementation |
| --- | --- |
| Interface | React, TypeScript, Three.js, custom GLSL particles |
| Controller | Python, FastAPI, Docker Engine |
| Model | `meta-models/muse-spark-open-1026`, served by Baseten |
| History and progress | SQLite, WebSocket, checksummed artifacts |
| Code checks | Isolated container, reproducing test before and after the change, existing application tests |

<details>
<summary>Operating limits</summary>

<br>

This local prototype supports up to three Docker applications and one active
investigation. Linkding is the repair case; Healthchecks was used to check
connection, observation and restart on a second application. Remote server fleets
and unattended production use have not been validated.

Code repair needs a configured source and test profile.

</details>

[Connection and repair profiles](docs/TARGET_MANAGEMENT.md) ·
[API](docs/OPS_API.md) · [Verification record](docs/FINAL_ACCEPTANCE.md) ·
[Still diagram](docs/readme/architecture.png)

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

[Setup and troubleshooting](docs/CLEAN_START.md) · [Hosted judging demo](docs/JUDGING_DEMO.md)

<br>

<div align="center">

Built for the Meta Global AI Developer Hackathon<br>
<sub>AURA and portrait artwork by Emir · <a href="LICENSE">MIT code</a> · <a href="MEDIA_LICENSE.md">Media terms</a> · <a href="THIRD_PARTY_NOTICES.md">Credits</a></sub>

</div>
