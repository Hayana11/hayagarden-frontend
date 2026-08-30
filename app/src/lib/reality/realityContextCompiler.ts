import type { RealitySnapshot, UserActivity } from "./realityStore";
import { getActivityFreshness } from "./realityStore";
import type { PhysicalMotion } from "./physicalMotion";

export type RealityPromptDynamicKey =
  | "lightExposure" | "orientation" | "motion" | "userActivity" | "charging" | "batteryLevel";

export type RealityPromptSegment =
  | { kind: "literal"; text: string }
  | { kind: "dynamic"; key: RealityPromptDynamicKey; text: string };

export interface CompiledRealityPrompt {
  schemaVersion: 1;
  text: string;
  segments: readonly RealityPromptSegment[];
}

type PromptClause = { key: RealityPromptDynamicKey; text: string };

export type ActivitySemanticConfidence = "hidden" | "low" | "normal";

export function lightSemanticLabel(
  light: RealitySnapshot["physical"]["facts"]["lightExposure"],
): string | null {
  if (light === "dark") return "较暗";
  if (light === "bright") return "较亮";
  return null;
}

export function orientationSemanticLabel(
  orientation: RealitySnapshot["physical"]["facts"]["orientation"],
): string | null {
  if (orientation === "face_up") return "正面朝上平放";
  if (orientation === "face_down") return "正面朝下扣放";
  if (orientation === "vertical") return "竖向";
  if (orientation === "horizontal") return "横向";
  return null;
}

export function getActivitySemanticConfidence(possibility: number | null): ActivitySemanticConfidence {
  if (typeof possibility !== "number" || !Number.isFinite(possibility) || possibility < 50) return "hidden";
  return possibility < 70 ? "low" : "normal";
}

function lightClause(snapshot: RealitySnapshot): PromptClause | null {
  const text = lightSemanticLabel(snapshot.physical.facts.lightExposure);
  return text === null ? null : { key: "lightExposure", text: `环境【${text}】` };
}

function orientationClause(snapshot: RealitySnapshot): PromptClause | null {
  const text = orientationSemanticLabel(snapshot.physical.facts.orientation);
  return text === null ? null : { key: "orientation", text: `姿态【${text}】` };
}

function motionClause(motion: PhysicalMotion): PromptClause | null {
  if (motion === "still") return { key: "motion", text: "设备当前【静止】" };
  if (motion === "moving") return { key: "motion", text: "设备当前【移动中】" };
  return null;
}

function activityClause(snapshot: RealitySnapshot, nowMs: number): PromptClause | null {
  const activity = snapshot.activity;
  const userActivity = activity?.userActivity;
  if (!activity || activity.source !== "hms" || userActivity === "unknown"
    || getActivityFreshness(snapshot, nowMs).status !== "fresh"
    || typeof activity.possibility !== "number" || !Number.isFinite(activity.possibility)) return null;

  const confidence = getActivitySemanticConfidence(activity.possibility);
  if (confidence === "hidden") return null;
  const labels: Record<Exclude<UserActivity, "unknown">, string> = {
    still: "静止", walking: "步行", running: "跑步", cycling: "骑行", in_vehicle: "车载",
  };
  const label = labels[userActivity];
  if (!label) return null;
  return { key: "userActivity", text: `推断活动【${label}${confidence === "low" ? "（低置信）" : ""}】` };
}

function compileClauses(snapshot: RealitySnapshot, nowMs: number): PromptClause[] {
  const clauses: PromptClause[] = [];
  const motion = motionClause(snapshot.physical.motion);
  if (motion !== null) clauses.push(motion);
  const activity = activityClause(snapshot, nowMs);
  if (activity !== null) clauses.push(activity);
  const light = lightClause(snapshot);
  if (light !== null) clauses.push(light);
  const orientation = orientationClause(snapshot);
  if (orientation !== null) clauses.push(orientation);
  if (snapshot.physical.facts.charging === true) clauses.push({ key: "charging", text: "正在充电" });
  const batteryLevel = snapshot.physical.facts.batteryLevel;
  if (typeof batteryLevel === "number" && Number.isInteger(batteryLevel) && batteryLevel >= 0 && batteryLevel <= 25) {
    clauses.push({ key: "batteryLevel", text: `电量【${batteryLevel}%】` });
  }
  return clauses;
}

export function compileRealityContext(snapshot: RealitySnapshot, nowMs: number = Date.now()): CompiledRealityPrompt {
  const clauses = compileClauses(snapshot, nowMs);
  if (clauses.length === 0) return { schemaVersion: 1, text: "", segments: [] };
  const segments: RealityPromptSegment[] = [];
  clauses.forEach((clause, index) => {
    if (index > 0) segments.push({ kind: "literal", text: "，" });
    segments.push({ kind: "dynamic", key: clause.key, text: clause.text });
  });
  segments.push({ kind: "literal", text: "。" });
  return { schemaVersion: 1, text: segments.map((segment) => segment.text).join(""), segments };
}
