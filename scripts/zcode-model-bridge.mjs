// Build against the user's installed ZCode runtime; no credentials are handled here.
import { createZCodeApp, startProcessProviderRegistryRuntime } from "@zcode/bootstrap";
import { ZCODE_VERSION } from "@zcode/shared";
import { prepareCliProviderRuntimeEnv } from "pdf-translate-zcode-cli-env";
import { readFileSync } from "node:fs";

class BridgeError extends Error {}
const identity = ref => ref ? `${ref.providerId}/${ref.modelId}` : "";

async function main() {
  const input = JSON.parse(readFileSync(0, "utf8"));
  if (!["models", "chat"].includes(input.action) || !input.workingDirectory) {
    throw new BridgeError("ZCode 运行桥请求无效");
  }
  const env = { HOME: process.env.HOME, PATH: process.env.PATH };
  Object.assign(env, await prepareCliProviderRuntimeEnv({
    argv: ["--prompt"], env, entrypoint: process.argv[1],
  }));
  const abort = new AbortController();
  const stop = () => abort.abort();
  process.once("SIGTERM", stop);
  process.once("SIGINT", stop);
  let registry, app;
  try {
    registry = await startProcessProviderRegistryRuntime(env, { standalone: {} });
    app = await createZCodeApp({
      env,
      providerRegistry: registry.runtime.registryService,
      configuredDefaultModelSelection: registry.configuredDefaultModelSelection,
      ...(registry.providerRuntimeHeadersPort ? { providerRuntimeHeadersPort: registry.providerRuntimeHeadersPort } : {}),
      ...(input.sessionId ? { sessionId: input.sessionId, resume: true } : {}),
      runtimeConfig: {
        mode: "yolo", workingDirectory: input.workingDirectory,
        dynamicWorkflowEnabled: false, memory: { extractionEnabled: false },
      },
      version: ZCODE_VERSION,
    });
    const models = app.listModels().filter(model => !model.disabledReason);
    const current = () => identity(app.runtime.getSessionModelSelection());
    if (input.action === "models") {
      return {
        current: current(),
        models: models.map(model => ({
          id: identity(model.ref), label: model.label,
          providerLabel: model.providerLabel || model.ref.providerId,
        })),
      };
    }
    if (input.model) {
      const selected = models.find(model => identity(model.ref) === input.model);
      if (!selected) throw new BridgeError("所选 ZCode 模型当前不可用，请刷新模型列表");
      const level = selected.reasoning?.defaultLevel;
      await app.setModel({
        ...selected.ref,
        ...(level ? { options: { ...selected.ref.options, reasoningLevel: level } } : {}),
      });
      if (current() !== input.model) throw new BridgeError("ZCode 未能应用所选模型");
    }
    if (!input.message?.trim()) throw new BridgeError("ZCode 问题不能为空");
    const prompt = input.attach
      ? { text: input.message, attachments: [{ type: "file", path: input.attach }] }
      : input.message;
    const result = await app.submitPrompt(prompt, { abortSignal: abort.signal });
    if (!result.response?.trim()) throw new BridgeError("ZCode 未返回回答");
    return { sessionId: String(app.sessionId), response: result.response, model: current() };
  } finally {
    try { await app?.close(); } finally { registry?.dispose(); }
    process.removeListener("SIGTERM", stop);
    process.removeListener("SIGINT", stop);
  }
}

main().then(
  result => process.stdout.write(JSON.stringify({ pdfTranslateZcodeBridge: 1, ...result }) + "\n"),
  error => {
    // Runtime diagnostics stay in ZCode's own logs; never echo credentials or raw stderr.
    const message = error instanceof BridgeError ? error.message : "ZCode 原生运行库调用失败，请检查本机 ZCode 日志";
    process.stdout.write(JSON.stringify({ pdfTranslateZcodeBridge: 1, error: message }) + "\n");
    process.exitCode = 1;
  },
);
