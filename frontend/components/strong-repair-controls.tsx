"use client";

import { useEffect } from "react";
import { OPENAI_REASONING_EFFORTS, OPENAI_VISION_MODELS } from "@/lib/model-options";

export type StrongRepairSelection = { model: string; effort: string };
const STORAGE_KEY = "ratsnest.strong-repair";

export function StrongRepairControls({ models, disabled, value, onChange }: {
  models: string[];
  disabled: boolean;
  value: StrongRepairSelection;
  onChange: (value: StrongRepairSelection) => void;
}) {
  useEffect(() => {
    if (!models.length) return;
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null");
      if (saved && models.includes(saved.model) && OPENAI_VISION_MODELS.includes(saved.model)
          && OPENAI_REASONING_EFFORTS[saved.model]?.includes(saved.effort)) onChange(saved);
    } catch { /* Invalid browser preference must never prevent a new run. */ }
  }, [models, onChange]);

  const select = (next: StrongRepairSelection) => {
    onChange(next);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  };
  return <>
    <div className="model-control">
      <div><label htmlFor="strong-repair-model">强模型升级执行器</label><span>REPAIR</span></div>
      <div className="select-shell">
        <i aria-hidden="true">S</i>
        <select id="strong-repair-model" disabled={disabled} value={value.model}
          onChange={(event) => {
            const model = event.target.value;
            const efforts = OPENAI_REASONING_EFFORTS[model] ?? [];
            select({ model, effort: efforts.includes("high") ? "high" : efforts[0] ?? "" });
          }}>
          <option value="">关闭</option>
          {OPENAI_VISION_MODELS.filter((model) => models.includes(model)).map((model) =>
            <option key={model} value={model}>{model}</option>)}
        </select>
      </div>
    </div>
    <div className="model-control">
      <div><label htmlFor="strong-repair-effort">升级执行推理强度</label><span>REPAIR</span></div>
      <div className="select-shell">
        <i aria-hidden="true">R</i>
        <select id="strong-repair-effort" disabled={disabled || !value.model} value={value.effort}
          onChange={(event) => select({ ...value, effort: event.target.value })}>
          {!value.model && <option value="">未启用</option>}
          {(OPENAI_REASONING_EFFORTS[value.model] ?? []).map((effort) =>
            <option key={effort} value={effort}>{effort}</option>)}
        </select>
      </div>
      <small>连续修复无改善时，在隔离副本中编程修复；额外计费。需管理员启用执行服务，仅新任务采用当前选择。</small>
    </div>
  </>;
}
