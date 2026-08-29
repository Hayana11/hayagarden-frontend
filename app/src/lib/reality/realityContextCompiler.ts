import type { RealitySnapshot, UserActivity } from "./realityStore";
import { getFreshUserActivity } from "./realityStore";
import type { PhysicalMotion } from "./physicalMotion";

export type RealityPromptDynamicKey =
  | "motion"
  | "userActivity"
  | "charging"
  | "batteryLevel";

export type RealityPromptSegment =
  | {
      kind: "literal";
      text: string;
    }
  | {
      kind: "dynamic";
      key: RealityPromptDynamicKey;
      text: string;
    };

export interface CompiledRealityPrompt {
  schemaVersion: 1;
  text: string;
  segments: readonly RealityPromptSegment[];
}

type PromptClause = {
  key: RealityPromptDynamicKey;
  text: string;
};

function motionClause(motion: PhysicalMotion): PromptClause | null {
  if (motion === "still") {
    return { key: "motion", text: "静止" };
  }

  if (motion === "moving") {
    return { key: "motion", text: "移动中" };
  }

  return null;
}

function userActivityClause(
  snapshot: RealitySnapshot,
  nowMs: number,
): PromptClause | null {
  const userActivity = getFreshUserActivity(snapshot, nowMs);
  if (userActivity === null) {
    return null;
  }

  const labels: Record<Exclude<UserActivity, "unknown">, string> = {
    still: "用户当前静止",
    walking: "用户正在步行",
    running: "用户正在跑步",
    cycling: "用户正在骑行",
    in_vehicle: "用户正在乘车",
  };

  return {
    key: "userActivity",
    text: labels[userActivity],
  };
}

function compileClauses(
  snapshot: RealitySnapshot,
  nowMs: number,
): PromptClause[] {
  const clauses: PromptClause[] = [];
  const motion = motionClause(snapshot.physical.motion);

  if (motion !== null) {
    clauses.push(motion);
  }

  const userActivity = userActivityClause(snapshot, nowMs);
  if (userActivity !== null) {
    clauses.push(userActivity);
  }

  if (snapshot.physical.facts.charging === true) {
    clauses.push({ key: "charging", text: "正在充电" });
  } else if (snapshot.physical.facts.charging === false) {
    clauses.push({ key: "charging", text: "未充电" });
  }

  const batteryLevel = snapshot.physical.facts.batteryLevel;
  if (
    typeof batteryLevel === "number" &&
    Number.isInteger(batteryLevel) &&
    batteryLevel >= 0 &&
    batteryLevel <= 100
  ) {
    clauses.push({
      key: "batteryLevel",
      text: `${batteryLevel}%`,
    });
  }

  return clauses;
}

export function compileRealityContext(
  snapshot: RealitySnapshot,
  nowMs: number = Date.now(),
): CompiledRealityPrompt {
  const clauses = compileClauses(snapshot, nowMs);

  if (clauses.length === 0) {
    return {
      schemaVersion: 1,
      text: "",
      segments: [],
    };
  }

  const segments: RealityPromptSegment[] = [
    { kind: "literal", text: "设备当前" },
  ];

  clauses.forEach((clause, index) => {
    if (clause.key === "batteryLevel") {
      segments.push({
        kind: "literal",
        text: index === 0 ? "电量" : "，电量",
      });
    } else if (index > 0) {
      segments.push({ kind: "literal", text: "，" });
    }

    segments.push({
      kind: "dynamic",
      key: clause.key,
      text: clause.text,
    });
  });

  segments.push({ kind: "literal", text: "。" });

  return {
    schemaVersion: 1,
    text: segments.map((segment) => segment.text).join(""),
    segments,
  };
}
