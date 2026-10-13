// Uses ZCode's own build configuration; the resulting native runtime stays local.
import { createRequire } from "node:module";
import { mkdtemp, rename, rm, writeFile, chmod } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const source = process.argv[2];
if (!source) throw new Error("用法: node scripts/build-zcode-model-bridge.mjs <ZCode 源码目录>");
const root = resolve(source);
const cli = join(root, "apps/zcode-cli/packages/cli");
const config = await import(pathToFileURL(join(cli, "scripts/build.mjs")));
const { build } = createRequire(join(root, "package.json"))("esbuild");
const output = join(cli, "dist");
const temporary = await mkdtemp(join(output, ".pdf-translate-bridge-"));
const bundle = join(output, "pdf-translate-model-bridge.cjs");
const launcher = join(output, "pdf-translate-model-bridge");
const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
try {
  await build({
    entryPoints: [join(dirname(fileURLToPath(import.meta.url)), "zcode-model-bridge.mjs")],
    bundle: true, platform: "node", format: "cjs", target: "node22", metafile: true,
    external: config.resolveBuildExternal(),
    alias: {
      ...config.resolveBuildAliases({ cliDirectory: cli, rootDirectory: join(root, "apps/zcode-cli") }),
      "@zcode/bootstrap": join(cli, "../bootstrap/src/index.ts"),
      "pdf-translate-zcode-cli-env": join(cli, "src/provider-runtime-env.ts"),
    },
    plugins: [config.createZodDedupePlugin({ expectedV4Version: await config.readZodBuildVersion() })],
    outfile: join(temporary, "bridge.cjs"), logLevel: "error",
  });
  await writeFile(join(temporary, "launcher"), `#!/bin/sh\nexec ${quote(process.execPath)} ${quote(bundle)} "$@"\n`);
  await chmod(join(temporary, "launcher"), 0o755);
  await rename(join(temporary, "bridge.cjs"), bundle);
  await rename(join(temporary, "launcher"), launcher);
  process.stdout.write(`ZCode 本机运行桥已构建: ${launcher}\n`);
} finally {
  await rm(temporary, { recursive: true, force: true });
}
