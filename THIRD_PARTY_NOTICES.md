# Credits and third-party material

AURA and the portrait artwork are by Emir. Development assistance came from
Codex and Muse Code. Runtime agents use Meta Muse Spark Open 1026 through Baseten;
Wonder is a development tool, not a runtime dependency.

| Component | Use | License or terms |
| --- | --- | --- |
| [React](https://github.com/facebook/react) | Interface | MIT |
| [Three.js](https://github.com/mrdoob/three.js) | Particle scene and postprocessing | [MIT](resources/references/threejs/LICENSE) |
| [Manrope](https://github.com/sharanda/manrope) | Interface typography | [SIL Open Font License 1.1](resources/licenses/manrope/LICENSE) |
| [psrdnoise](https://github.com/stegu/psrdnoise) | GLSL noise | MIT; notice retained in the shader and [upstream README](resources/vendor/psrdnoise/README.md) |
| [Depth Anything V2 Small](https://github.com/DepthAnything/Depth-Anything-V2) | Offline portrait depth preparation | [Apache 2.0](resources/licenses/depth-anything-v2/LICENSE); model weights are not distributed here |
| [Linkding](https://github.com/sissbruecker/linkding) | Independent application used for repair trials | [MIT](resources/ops/LICENSE.linkding.txt) |
| [Healthchecks](https://github.com/healthchecks/healthchecks) | Second application used for integration checks | [BSD 3-Clause](resources/ops/LICENSE.healthchecks.txt) |
| [Cat Walk by Arulo](https://mixkit.co/free-stock-music/tag/fashion/) | Promotional film soundtrack | [Mixkit Stock Music Free License](https://mixkit.co/license/#musicFree) |

Python and npm dependencies retain their own licenses and are installed from
the committed lockfiles. Their package directories are not bundled in this
source repository. Linkding and Healthchecks are independent projects; this
integration does not imply their endorsement.

Pinned research and development references are recorded in
[resources/sources.json](resources/sources.json). Local development skills,
downloaded reference repositories and model weights are not required to run AURA.
Preserved upstream notices remain under `resources/`.

See [media terms](MEDIA_LICENSE.md) for the portrait, original audio and film.
