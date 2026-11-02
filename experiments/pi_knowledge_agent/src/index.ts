import { runPiKnowledgeAgent } from "./agent.js";

function parseArgs(argv: string[]): { question: string; json: boolean } {
  const json = argv.includes("--json");
  const index = argv.indexOf("--question");
  const question =
    index >= 0
      ? (argv[index + 1] ?? "")
      : argv.filter((arg) => !arg.startsWith("--")).join(" ");
  return { question, json };
}

const { question, json } = parseArgs(process.argv.slice(2));
if (!question.trim()) {
  console.error("usage: node dist/index.js --question <question> [--json]");
  process.exit(2);
}

const result = await runPiKnowledgeAgent(question);
if (json) {
  console.log(JSON.stringify(result));
} else {
  console.log(result.answer);
  if (result.sources.length > 0) {
    console.log(`\n参考来源：\n${result.sources.join("\n")}`);
  }
}
if (result.error) {
  process.exit(1);
}
