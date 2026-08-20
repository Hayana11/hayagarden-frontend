import type { RealitySnapshot } from "./realityStore";
import type { PhysicalMotion } from "./physicalMotion";

export type RealityPromptDynamicKey =
  | "motion"
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

function compileClauses(snapshot: RealitySnapshot): PromptClause[] {
  const clauses: PromptClause[] = [];
  const motion = motionClause(snapshot.physical.motion);

  if (motion !== null) {
    clauses.push(motion);
  }

  if (snapshot.physical.facts.charging === true) {
    clauses.push({ key: "charging", text: "正在充电" });
  } else if (snapshot.physical.facts.charging === false) {
    clauses.push({ key: "charging", text: "未充电" });
  }

  const batteryLevel = snapshot.physical.facts.batteryLevel;
  if (
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
): CompiledRealityPrompt {
  const clauses = compileClauses(snapshot);

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
