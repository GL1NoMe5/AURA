varying vec3 vTint;
varying float vAlpha;
varying vec2 vGrain;
void main() {
  vec2 grain = (gl_PointCoord - 0.5) * 2.0;
  float angle = vGrain.x * 6.2831853;
  grain = mat2(cos(angle), -sin(angle), sin(angle), cos(angle)) * grain;
  grain.x *= mix(1.0, 1.12, vGrain.y);
  float d = length(grain);
  if (d > 1.0) discard;
  float softness = exp(-d * d * 3.7) * (1.0 - smoothstep(0.65, 1.0, d));
  // Slightly firmer, uneven grains, while retaining the existing soft light.
  float grainCore = exp(-d * d * 2.8) * (1.0 - smoothstep(0.52, 0.94, d));
  float shape = mix(softness, grainCore, vGrain.y * 0.38);
  gl_FragColor = vec4(vTint * 2.2, shape * vAlpha);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
}
