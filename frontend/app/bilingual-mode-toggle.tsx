"use client";

import { useEffect, useState } from "react";

import { BILINGUAL_MODE_CHANGE_EVENT, UI_STORAGE_KEYS } from "./storage-keys";

function storedBilingualMode() {
  return window.localStorage.getItem(UI_STORAGE_KEYS.readingMode) !== "original";
}

export function BilingualModeToggle() {
  const [enabled, setEnabled] = useState(true);

  useEffect(() => {
    const syncFromStorage = () => setEnabled(storedBilingualMode());
    const syncFromPreferenceEvent = (event: Event) => {
      const detail = (event as CustomEvent<{ enabled?: boolean }>).detail;
      setEnabled(typeof detail?.enabled === "boolean" ? detail.enabled : storedBilingualMode());
    };
    syncFromStorage();
    window.addEventListener("storage", syncFromStorage);
    window.addEventListener(BILINGUAL_MODE_CHANGE_EVENT, syncFromPreferenceEvent);
    return () => {
      window.removeEventListener("storage", syncFromStorage);
      window.removeEventListener(BILINGUAL_MODE_CHANGE_EVENT, syncFromPreferenceEvent);
    };
  }, []);

  function toggle() {
    const nextEnabled = !enabled;
    setEnabled(nextEnabled);
    window.localStorage.setItem(
      UI_STORAGE_KEYS.readingMode,
      nextEnabled ? "bilingual" : "original",
    );
    window.dispatchEvent(new CustomEvent(BILINGUAL_MODE_CHANGE_EVENT, {
      detail: { enabled: nextEnabled },
    }));
  }

  return (
    <button
      aria-checked={enabled}
      aria-label="双语对照模式"
      className="topbar-bilingual-toggle"
      onClick={toggle}
      role="switch"
      title={enabled ? "隐藏中文翻译区块" : "显示中文翻译区块"}
      type="button"
    >
      <span aria-hidden="true" className="topbar-bilingual-track">
        <span />
      </span>
      <span className="topbar-bilingual-label">
        <span className="topbar-bilingual-label-full">双语对照模式</span>
        <span className="topbar-bilingual-label-compact">双语</span>
      </span>
    </button>
  );
}
