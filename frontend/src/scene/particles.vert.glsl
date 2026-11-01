uniform float uTime;
uniform float uVisibleCount;
attribute float aRank;
uniform float uPortrait;
uniform float uMotion;
attribute vec2 aDisplacement;
uniform float uBrushEnabled;
uniform float uPixelRatio;
uniform vec4 uHole;
uniform float uBurstGlow;
attribute vec3 aStart;
attribute vec3 aControl1;
attribute vec3 aControl2;
attribute vec3 aEnd;
attribute vec4 aSeed;
attribute vec3 aTint;
attribute vec3 aPortrait;
attribute vec4 aPortraitData;
attribute vec3 aPortraitColor;
varying vec3 vTint;
varying float vAlpha;
varying vec2 vGrain;

vec3 curve(float t) {
  float q = 1.0 - t;
  return q*q*q*aStart + 3.0*q*q*t*aControl1 + 3.0*q*t*t*aControl2 + t*t*t*aEnd;
}

void main() {
  float morph = smoothstep(0.0, 1.0, uPortrait);
  // At the full portrait the stream path is mixed away completely, so its
  // curve/noise work is skipped; transitions run the original path.
  vec3 p = vec3(0.0);
  float wave = 0.0;
  float noise = 0.0;
  if (morph < 1.0) {
    float t = fract(aSeed.x + uTime * (0.025 + aSeed.y * 0.018));
    wave = sin(t * 3.14159265);
    p = curve(t);
    float strand = aSeed.z * 6.2831853;
    vec3 gradient;
    noise = psrdnoise(p * 0.5 + vec3(aSeed.y * 2.0, 0.0, uTime * 0.045), vec3(0.0), uTime * 0.08, gradient);
    float width = (0.035 + aSeed.w * aSeed.w * 0.24) * (0.3 + wave);
    p += vec3(cos(strand + t * 9.0), sin(strand + t * 9.0), sin(strand * 2.0)) * width;
    p += cross(gradient, vec3(0.25, 0.6, 0.45)) * 0.065 * wave;

    // Sparse particles stay around the streams, giving depth without a starfield.
    if (aSeed.w > 0.975) {
      p = mix(vec3((aSeed.x-0.5)*12.0, (aSeed.y-0.5)*6.7, sin(strand)*2.0), p, 0.25);
    }
  }
  vGrain = vec2(aSeed.z, morph);
  vec3 face = aPortrait;
  float breath = sin(uTime * 1.05) * uMotion;
  face.xy *= 1.0 + breath * 0.003;
  face.y += breath * 0.012;
  // Keep the source eye shape fixed: blinking was removed at the user's request.
  float turn = sin(uTime * 0.23) * 0.012 * uMotion;
  face.xz = mat2(cos(turn), -sin(turn), sin(turn), cos(turn)) * face.xz;
  face *= 1.38;
  // Persistent integrated flow, rather than expiring radial impulses.
  face.xy += aDisplacement * uBrushEnabled;
  p = mix(p, face, morph);
  p.z += sin(aSeed.x * 6.2831853) * morph * (1.0 - morph) * 1.4;
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = clamp((0.8 + 1.5 * pow(aSeed.y, 4.0)) * (10.0 / -mv.z) * uPixelRatio, 0.65, 4.0);
  vTint = aTint;
  vAlpha = (0.1 + 0.34 * aSeed.y) * (0.45 + 0.55 * wave) * (0.9 + noise * 0.1);
  if (aSeed.w > 0.975) vAlpha *= 0.35;
  float grainSize = mix(0.82, 1.16, aSeed.w);
  gl_PointSize = mix(gl_PointSize, (1.25 + aPortraitData.z * 0.95) * grainSize * (11.5 / -mv.z) * uPixelRatio, morph);
  vTint = mix(vTint, aPortraitColor, morph);
  vTint *= mix(1.0, 0.90 + aSeed.z * 0.18, morph);
  // Source RGB already carries shadows; avoid attenuating them a second time.
  float matte = clamp(aPortraitData.w / (0.10 + 0.90 * pow(aPortraitData.z, 1.2)), 0.0, 1.0);
  vAlpha = mix(vAlpha, matte * 0.80, morph);
  float orbitDistance = length(vec2(face.x - uHole.x, (face.y - uHole.y) / 0.40));
  float orbitLight = exp(-abs(orbitDistance - 0.68) * 2.5) * uHole.z;
  float energy = (orbitLight + uBurstGlow * 0.20) * morph;
  vTint = mix(vTint, vec3(1.8, 1.02, 0.38), min(0.88, energy));
  vTint += vec3(1.3, 0.72, 0.28) * energy;
  // Many grains converge onto a small disk; keep the individual grains legible.
  vAlpha = mix(vAlpha, matte * 0.10, min(0.96, orbitLight * morph));
  gl_PointSize *= 1.0 + energy * 0.45;
  vAlpha *= 1.0 - smoothstep(uVisibleCount - 800.0, uVisibleCount, aRank);
}
