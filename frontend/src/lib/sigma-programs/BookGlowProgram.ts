import { NodeProgram } from "sigma/rendering";
import type { NodeDisplayData, RenderParams } from "sigma/types";
import type { ProgramInfo } from "sigma/rendering";
import { floatColor } from "sigma/utils";

const FLOAT = 5126;
const UNSIGNED_BYTE = 5121;
const TRIANGLES = 4;

const VERTEX_SHADER_SOURCE = `
attribute vec4 a_id;
attribute vec4 a_color;
attribute vec2 a_position;
attribute float a_size;
attribute float a_angle;
attribute float a_bridgeCount;

uniform mat3 u_matrix;
uniform float u_sizeRatio;
uniform float u_correctionRatio;
uniform float u_time;

varying vec4 v_color;
varying vec2 v_diffVector;
varying float v_radius;
varying float v_coreRadius;
varying float v_glow;

const float bias = 255.0 / 254.0;

void main() {
  float bridgeGlow = min(max(a_bridgeCount, 0.0) / 20.0, 1.0);
  float breath = 0.97 + 0.03 * sin(u_time * 0.8 + a_position.x * 0.04 + a_position.y * 0.025);
  float haloMultiplier = mix(3.2, 4.8, bridgeGlow) * breath;
  float size = a_size * haloMultiplier * u_correctionRatio / u_sizeRatio * 4.0;
  vec2 diffVector = size * vec2(cos(a_angle), sin(a_angle));
  vec2 position = a_position + diffVector;

  gl_Position = vec4((u_matrix * vec3(position, 1)).xy, 0, 1);

  v_diffVector = diffVector;
  v_radius = size / 2.0;
  v_coreRadius = v_radius / haloMultiplier;
  v_glow = 0.16 + bridgeGlow * 0.24;

  #ifdef PICKING_MODE
  v_color = a_id;
  #else
  v_color = a_color;
  #endif

  v_color.a *= bias;
}
`;

const FRAGMENT_SHADER_SOURCE = `
precision highp float;

varying vec4 v_color;
varying vec2 v_diffVector;
varying float v_radius;
varying float v_coreRadius;
varying float v_glow;

uniform float u_correctionRatio;

const vec4 transparent = vec4(0.0, 0.0, 0.0, 0.0);

void main(void) {
  float border = u_correctionRatio * 2.0;
  float dist = length(v_diffVector);

  #ifdef PICKING_MODE
  if (dist > v_coreRadius + border * 2.0)
    gl_FragColor = transparent;
  else
    gl_FragColor = v_color;
  #else
  float coreAlpha = 1.0 - smoothstep(v_coreRadius - border, v_coreRadius + border, dist);
  float innerGlow = 1.0 - smoothstep(0.0, v_radius * 0.72, dist);
  float outerGlow = 1.0 - smoothstep(v_coreRadius, v_radius, dist);
  float alpha = max(coreAlpha, outerGlow * v_glow + innerGlow * 0.08);

  if (alpha <= 0.002) {
    gl_FragColor = transparent;
  } else {
    vec3 rgb = v_color.rgb * (0.88 + coreAlpha * 0.34);
    gl_FragColor = vec4(rgb * alpha, alpha);
  }
  #endif
}
`;

const UNIFORMS = [
  "u_sizeRatio",
  "u_correctionRatio",
  "u_matrix",
  "u_time",
] as const;

type BookGlowUniform = (typeof UNIFORMS)[number];

export default class BookGlowProgram extends NodeProgram<BookGlowUniform> {
  static readonly ANGLE_1 = 0;
  static readonly ANGLE_2 = (2 * Math.PI) / 3;
  static readonly ANGLE_3 = (4 * Math.PI) / 3;

  getDefinition() {
    return {
      VERTICES: 3,
      VERTEX_SHADER_SOURCE,
      FRAGMENT_SHADER_SOURCE,
      METHOD: TRIANGLES,
      UNIFORMS,
      ATTRIBUTES: [
        { name: "a_position", size: 2, type: FLOAT },
        { name: "a_size", size: 1, type: FLOAT },
        { name: "a_color", size: 4, type: UNSIGNED_BYTE, normalized: true },
        { name: "a_id", size: 4, type: UNSIGNED_BYTE, normalized: true },
        { name: "a_bridgeCount", size: 1, type: FLOAT },
      ],
      CONSTANT_ATTRIBUTES: [{ name: "a_angle", size: 1, type: FLOAT }],
      CONSTANT_DATA: [
        [BookGlowProgram.ANGLE_1],
        [BookGlowProgram.ANGLE_2],
        [BookGlowProgram.ANGLE_3],
      ],
    };
  }

  processVisibleItem(
    nodeIndex: number,
    startIndex: number,
    data: NodeDisplayData,
  ): void {
    const array = this.array;
    const bridgeCount = Math.max(0, Number((data as any).bridge_count ?? 1));
    array[startIndex++] = data.x;
    array[startIndex++] = data.y;
    array[startIndex++] = data.size;
    array[startIndex++] = floatColor(data.color);
    array[startIndex++] = nodeIndex;
    array[startIndex] = bridgeCount;
  }

  setUniforms(
    params: RenderParams,
    { gl, uniformLocations }: ProgramInfo<BookGlowUniform>,
  ): void {
    gl.uniform1f(uniformLocations.u_correctionRatio, params.correctionRatio);
    gl.uniform1f(uniformLocations.u_sizeRatio, params.sizeRatio);
    gl.uniformMatrix3fv(uniformLocations.u_matrix, false, params.matrix);
    gl.uniform1f(uniformLocations.u_time, performance.now() / 1000);
  }
}
