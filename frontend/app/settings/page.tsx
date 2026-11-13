"use client";

import { useEffect, useState } from "react";

import {
  addPaperMapQuestion,
  deletePaperMapQuestion,
  getPaperMapQuestionLibrary,
  getSettingsStatus,
  resetTranslationSystemPrompt,
  updateAutomaticAiRepair,
  updateAutomaticTranslation,
  updatePaperMapQuestion,
  updateTranslationSystemPrompt,
  type SettingsStatus,
  type PaperMapQuestionLibraryItem,
} from "../api";
import { ProtectedLlmCredentialSettings } from "../llm-credential-form";
import { isDesktopSupervisorAvailable } from "../runtime-capabilities";
import { useRuntimeCapabilities } from "../runtime-capability-provider";

export default function SettingsPage() {
  const runtimeCapabilities = useRuntimeCapabilities();
  const [status, setStatus] = useState<SettingsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [promptDraft, setPromptDraft] = useState("");
  const [promptSaving, setPromptSaving] = useState(false);
  const [promptMessage, setPromptMessage] = useState<string | null>(null);
  const [autoRepairSaving, setAutoRepairSaving] = useState(false);
  const [autoRepairMessage, setAutoRepairMessage] = useState<string | null>(null);
  const [autoTranslationSaving, setAutoTranslationSaving] = useState(false);
  const [autoTranslationMessage, setAutoTranslationMessage] = useState<string | null>(null);
  const [mapQuestions, setMapQuestions] = useState<PaperMapQuestionLibraryItem[]>([]);
  const [mapQuestionDraft, setMapQuestionDraft] = useState("");
  const [mapQuestionBusy, setMapQuestionBusy] = useState(false);
  const [mapQuestionMessage, setMapQuestionMessage] = useState<string | null>(null);
  const [editingQuestionKey, setEditingQuestionKey] = useState<string | null>(null);
  const [editingQuestionDraft, setEditingQuestionDraft] = useState("");
  const [quitPending, setQuitPending] = useState(false);
  const [quitMessage, setQuitMessage] = useState<string | null>(null);

  useEffect(() => {
    void Promise.all([getSettingsStatus(), getPaperMapQuestionLibrary()])
      .then(([nextStatus, library]) => {
        setStatus(nextStatus);
        setPromptDraft(nextStatus.translation_system_prompt);
        setMapQuestions(library.questions);
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "Status unavailable."));
  }, []);

  async function createMapQuestion() {
    setMapQuestionBusy(true);
    setMapQuestionMessage(null);
    try {
      const library = await addPaperMapQuestion(mapQuestionDraft);
      setMapQuestions(library.questions);
      setMapQuestionDraft("");
      setMapQuestionMessage("Question added. It will be considered when future maps are generated.");
    } catch (reason) {
      setMapQuestionMessage(reason instanceof Error ? reason.message : "Unable to add question.");
    } finally {
      setMapQuestionBusy(false);
    }
  }

  async function removeMapQuestion(key: string) {
    setMapQuestionBusy(true);
    setMapQuestionMessage(null);
    try {
      const library = await deletePaperMapQuestion(key);
      setMapQuestions(library.questions);
      setMapQuestionMessage("Question removed.");
    } catch (reason) {
      setMapQuestionMessage(reason instanceof Error ? reason.message : "Unable to remove question.");
    } finally {
      setMapQuestionBusy(false);
    }
  }

  function beginEditingQuestion(item: PaperMapQuestionLibraryItem) {
    if (mapQuestionBusy) return;
    setEditingQuestionKey(item.key);
    setEditingQuestionDraft(item.text);
    setMapQuestionMessage(null);
  }

  async function saveMapQuestion(key: string) {
    setMapQuestionBusy(true);
    setMapQuestionMessage(null);
    try {
      const library = await updatePaperMapQuestion(key, editingQuestionDraft);
      setMapQuestions(library.questions);
      setEditingQuestionKey(null);
      setEditingQuestionDraft("");
      setMapQuestionMessage("Question saved.");
    } catch (reason) {
      setMapQuestionMessage(reason instanceof Error ? reason.message : "Unable to save question.");
    } finally {
      setMapQuestionBusy(false);
    }
  }

  async function saveTranslationPrompt() {
    setPromptSaving(true);
    setPromptMessage(null);
    try {
      const result = await updateTranslationSystemPrompt(promptDraft);
      setPromptDraft(result.system_prompt);
      setStatus((current) => current ? {
        ...current,
        translation_system_prompt: result.system_prompt,
      } : current);
      setPromptMessage("已保存。之后提交的翻译将使用此提示词，已有译文保持不变。");
    } catch (reason) {
      setPromptMessage(reason instanceof Error ? reason.message : "无法保存翻译提示词。");
    } finally {
      setPromptSaving(false);
    }
  }

  async function changeAutomaticAiRepair(enabled: boolean) {
    setAutoRepairSaving(true);
    setAutoRepairMessage(null);
    try {
      const result = await updateAutomaticAiRepair(enabled);
      setStatus((current) => current ? {
        ...current,
        automatic_ai_repair_enabled: result.automatic_ai_repair_enabled,
      } : current);
      setAutoRepairMessage(
        result.automatic_ai_repair_enabled
          ? "已开启。之后符合条件的排版失败可能自动提交 AI 辅助修复任务。"
          : "已关闭。需要时可在阅读页手动发起修复。",
      );
    } catch (reason) {
      setAutoRepairMessage(
        reason instanceof Error ? reason.message : "无法更新自动修复设置。",
      );
    } finally {
      setAutoRepairSaving(false);
    }
  }

  async function changeAutomaticTranslation(enabled: boolean) {
    setAutoTranslationSaving(true);
    setAutoTranslationMessage(null);
    try {
      const result = await updateAutomaticTranslation(enabled);
      setStatus((current) => current ? {
        ...current,
        automatic_translation_enabled: result.automatic_translation_enabled,
      } : current);
      setAutoTranslationMessage(
        result.automatic_translation_enabled
          ? "已开启。以后导入并解析完成的论文会自动提交 LLM 翻译任务。"
          : "已关闭。新论文只解析英文原文，仍可在阅读页手动翻译。",
      );
    } catch (reason) {
      setAutoTranslationMessage(
        reason instanceof Error ? reason.message : "无法更新自动翻译设置。",
      );
    } finally {
      setAutoTranslationSaving(false);
    }
  }

  async function restoreDefaultTranslationPrompt() {
    if (!window.confirm(
      "恢复内置翻译系统提示词？这会替换当前账号的自定义提示词。",
    )) return;
    setPromptSaving(true);
    setPromptMessage(null);
    try {
      const result = await resetTranslationSystemPrompt();
      setPromptDraft(result.system_prompt);
      setStatus((current) => current ? {
        ...current,
        translation_system_prompt: result.system_prompt,
      } : current);
      setPromptMessage("已恢复内置翻译系统提示词。");
    } catch (reason) {
      setPromptMessage(reason instanceof Error ? reason.message : "无法恢复翻译提示词。");
    } finally {
      setPromptSaving(false);
    }
  }

  async function refreshSavedCredentialStatus() {
    try {
      setStatus(await getSettingsStatus());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "凭据已保存，但暂时无法刷新配置状态。");
    }
  }

  async function quitLocalApplication() {
    if (!window.confirm(
      "Quit AcadeMate Paper? Background imports and other local work will be drained before the application stops.",
    )) return;
    setQuitPending(true);
    setQuitMessage(null);
    try {
      const response = await fetch("/api/local-runtime/quit", {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
      });
      setQuitMessage(
        response.status === 202
          ? "Quit accepted. AcadeMate Paper is shutting down safely."
          : "The local Supervisor did not accept the quit request.",
      );
    } catch {
      setQuitMessage("The quit request could not be confirmed.");
    } finally {
      setQuitPending(false);
    }
  }

  return (
    <div className="settings-page">
      <header className="page-title">
        <p className="eyebrow">个人配置</p>
        <h1>个人设置</h1>
        <p>管理当前账号的模型凭据、翻译和辅助修复偏好。</p>
        <a href="/developer-docs/index.html" rel="noreferrer" target="_blank">开发者 API 文档</a>
      </header>
      {error ? <p className="notice" role="alert">{error}</p> : null}
      {isDesktopSupervisorAvailable(runtimeCapabilities) ? (
        <section className="settings-card">
          <div className="setting-row">
            <div>
              <strong>Local application</strong>
              <span>Stop the local server and drain its managed worker and child processes.</span>
            </div>
            <button
              disabled={quitPending}
              onClick={() => void quitLocalApplication()}
              type="button"
            >
              {quitPending ? "Quitting…" : "Quit AcadeMate Paper"}
            </button>
          </div>
          {quitMessage ? <p role="status">{quitMessage}</p> : null}
        </section>
      ) : null}
      <ProtectedLlmCredentialSettings
        onSaved={() => void refreshSavedCredentialStatus()}
      />
      <section className="settings-card automatic-toggle-settings-card">
        <label className="automatic-toggle-setting">
          <span>
            <strong>自动翻译</strong>
            <small>
              默认关闭。开启后，新论文在导入和结构解析完成后会自动提交 LLM 翻译任务，可能产生模型费用；
              关闭时不会自动调用 LLM，阅读页中的手动单块翻译和整篇翻译入口仍然保留。
            </small>
          </span>
          <input
            aria-label="文章导入后自动提交 LLM 翻译"
            checked={status?.automatic_translation_enabled ?? false}
            disabled={status === null || autoTranslationSaving}
            onChange={(event) => void changeAutomaticTranslation(event.target.checked)}
            type="checkbox"
          />
        </label>
        {autoTranslationMessage ? (
          <p className="automatic-toggle-message" role="status">{autoTranslationMessage}</p>
        ) : null}
      </section>
      <section className="settings-card automatic-toggle-settings-card">
        <label className="automatic-toggle-setting">
          <span>
            <strong>自动 AI 辅助修复</strong>
            <small>
              开启后，符合条件的表格或内容块排版失败可能自动提交 AI 修复任务，可能产生模型费用。
              公式仍会先尝试本地排版；关闭后，仅在你手动发起修复时使用 AI。
            </small>
          </span>
          <input
            aria-label="自动提交排版失败内容的 AI 辅助修复"
            checked={status?.automatic_ai_repair_enabled ?? false}
            disabled={status === null || autoRepairSaving}
            onChange={(event) => void changeAutomaticAiRepair(event.target.checked)}
            type="checkbox"
          />
        </label>
        {autoRepairMessage ? <p className="automatic-toggle-message" role="status">{autoRepairMessage}</p> : null}
      </section>
      <section className="settings-card prompt-settings-card">
        <div className="prompt-settings-heading">
          <div>
            <strong>个人翻译系统提示词</strong>
            <p>
              用于你的自动翻译，并作为重新翻译单个内容块时的默认提示词。
              已完成的翻译不会改变，除非你主动重新翻译。
            </p>
          </div>
          <small>{promptDraft.length}/8000</small>
        </div>
        <textarea
          aria-label="个人翻译系统提示词"
          maxLength={8000}
          minLength={80}
          onChange={(event) => setPromptDraft(event.target.value)}
          rows={12}
          value={promptDraft}
        />
        <div className="prompt-settings-actions">
          <button
            disabled={promptSaving || promptDraft.trim().length < 80}
            onClick={() => void saveTranslationPrompt()}
            type="button"
          >
            {promptSaving ? "正在保存…" : "保存翻译提示词"}
          </button>
          <button
            aria-label="恢复默认翻译系统提示词"
            className="prompt-reset-button"
            disabled={promptSaving}
            onClick={() => void restoreDefaultTranslationPrompt()}
            type="button"
          >
            恢复原状
          </button>
          {promptMessage ? <span role="status">{promptMessage}</span> : null}
        </div>
      </section>
      <section className="settings-card paper-map-question-settings">
        <div className="prompt-settings-heading">
          <div>
            <strong>Paper map initial question library</strong>
            <p>
              The planner semantically selects only questions applicable to each paper. Click any
              question to edit it; changes apply to maps generated in the future.
            </p>
          </div>
          <small>{mapQuestions.filter((item) => item.user_defined).length}/40 custom</small>
        </div>
        <div className="paper-map-question-add">
          <input
            aria-label="New paper map initial question"
            maxLength={500}
            onChange={(event) => setMapQuestionDraft(event.target.value)}
            placeholder="Add a question that may be useful for some papers…"
            value={mapQuestionDraft}
          />
          <button
            disabled={mapQuestionBusy || mapQuestionDraft.trim().length < 8}
            onClick={() => void createMapQuestion()}
            type="button"
          >Add question</button>
        </div>
        <div className="paper-map-question-library">
          {mapQuestions.map((item) => (
            <div className="paper-map-question-library-item" key={item.key}>
              <div
                className="paper-map-question-text"
                onClick={() => beginEditingQuestion(item)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") beginEditingQuestion(item);
                }}
                role="button"
                tabIndex={0}
              >
                <small>{item.user_defined ? "CUSTOM" : item.dimension.replaceAll("_", " ")}</small>
                {editingQuestionKey === item.key ? (
                  <textarea
                    aria-label={`Edit question: ${item.text}`}
                    autoFocus
                    maxLength={500}
                    onClick={(event) => event.stopPropagation()}
                    onChange={(event) => setEditingQuestionDraft(event.target.value)}
                    rows={2}
                    value={editingQuestionDraft}
                  />
                ) : <span>{item.text}</span>}
              </div>
              <button
                aria-label={editingQuestionKey === item.key
                  ? `Save question: ${item.text}`
                  : `Delete question: ${item.text}`}
                className={editingQuestionKey === item.key ? "question-save-button" : "question-delete-button"}
                disabled={mapQuestionBusy || (
                  editingQuestionKey === item.key && editingQuestionDraft.trim().length < 8
                )}
                onClick={() => editingQuestionKey === item.key
                  ? void saveMapQuestion(item.key)
                  : void removeMapQuestion(item.key)}
                title={editingQuestionKey === item.key ? "Save question" : "Delete question"}
                type="button"
              >{editingQuestionKey === item.key ? "✓" : "×"}</button>
            </div>
          ))}
        </div>
        {mapQuestionMessage ? <span role="status">{mapQuestionMessage}</span> : null}
      </section>
    </div>
  );
}
