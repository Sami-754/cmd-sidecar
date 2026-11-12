export default function(cmd) {
  cmd.on('run_start', () => {
    cmd.setActiveTools([]);
  });
  cmd.hooks({
    transformSystemPrompt: () => "You are a pure AI model with no tools. Answer directly.",
    beforeToolCall: async () => ({ block: true, terminate: true })
  });
}
