// V.O.I.D Singularity — fullscreen quad vertex shader (reference copy).
// Runtime-identical to window.VOID_SHADERS.vertex in ../shaders.js
varying vec2 vUv;
void main(){
  vUv = uv;
  gl_Position = vec4(position, 1.0);
}
