import { AutoProcessor, AutoModelForCausalLM, TextStreamer, env } from "@huggingface/transformers";

// Disable local models to force loading from HF Hub
env.allowLocalModels = false;

let model: any = null;
let processor: any = null;

const MODEL_ID = "onnx-community/gemma-4-E4B-it-ONNX";

async function loadModel() {
  if (model && processor) return;

  self.postMessage({ type: 'status', message: 'Loading processor...' });
  processor = await AutoProcessor.from_pretrained(MODEL_ID);

  self.postMessage({ type: 'status', message: 'Loading model...' });
  model = await AutoModelForCausalLM.from_pretrained(MODEL_ID, {
    dtype: "q4f16", // Recommended quantization for performance on consumer hardware
    device: "webgpu", // Utilize WebGPU
    progress_callback: (info: any) => {
      self.postMessage({ type: 'progress', info });
    },
  });

  self.postMessage({ type: 'status', message: 'Ready' });
}

self.addEventListener('message', async (e) => {
  const { type, messages } = e.data;

  if (type === 'load') {
    await loadModel();
    return;
  }

  if (type === 'generate') {
    if (!model || !processor) {
      self.postMessage({ type: 'error', message: 'Model not loaded' });
      return;
    }

    try {
      // Setup the prompt
      const prompt = processor.apply_chat_template(messages, {
        enable_thinking: true, // Enable reasoning mode
        add_generation_prompt: true,
      });

      const inputs = await processor(prompt, undefined, undefined, {
        add_special_tokens: false,
      });

      // Simple TextStreamer setup to emit partial results
      const streamer = new TextStreamer(processor.tokenizer, {
        skip_prompt: true,
        skip_special_tokens: false,
        callback_function: (text: string) => {
          self.postMessage({ type: 'chunk', text });
        },
      });

      self.postMessage({ type: 'start_generation' });

      // Generate the output
      const outputs = await model.generate({
        ...inputs,
        max_new_tokens: 1024,
        do_sample: false,
        streamer: streamer,
      });

      const decoded = processor.batch_decode(
        outputs.slice(null, [inputs.input_ids.dims.at(-1), null]),
        { skip_special_tokens: true },
      );

      self.postMessage({ type: 'complete', text: decoded[0] });
    } catch (err: any) {
      self.postMessage({ type: 'error', message: err.message });
    }
  }
});
