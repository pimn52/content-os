import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';

const file = fileURLToPath(new URL('../src/RuntimeSettingsPanel.tsx', import.meta.url));

test('runtime configuration saves file references and becomes active after restart', async () => {
  const calls = [];
  const request = async (path, options) => {
    calls.push({ path, options });
    if (!options) return { planner_model: 'model-a', planner_key_file: 'C:\\keys\\planner.txt' };
    return JSON.parse(options.body);
  };
  const h = componentHarness(file, 'RuntimeSettingsPanel', { request });
  await h.flush();
  assert.equal(h.input('规划模型').props.value, 'model-a');
  const advanced = h.nodes('details')[0];
  assert.ok(advanced);
  assert.notEqual(advanced.props.open, true);
  assert.equal(h.find('summary', '高级选项').type, 'summary');
  await h.change('视觉凭据文件路径', 'C:\\keys\\vision.txt');
  await h.change('图库凭据文件路径', 'C:\\keys\\stock.txt');
  await h.change('视觉思考模式', 'disabled');
  await h.change('规划推理强度', 'low');
  await h.change('规划输出上限', '8192');
  await h.change('规划超时时间', '120');
  await h.change('声音配置文件路径', 'C:\\models\\voice.json');
  await h.submit('保存运行配置');
  const saved = calls.find((call) => call.options?.method === 'PUT');
  const body = JSON.parse(saved.options.body);
  assert.equal(body.planner_key_file, 'C:\\keys\\planner.txt');
  assert.equal(body.vision_key_file, 'C:\\keys\\vision.txt');
  assert.equal(body.pexels_key_file, 'C:\\keys\\stock.txt');
  assert.equal(body.vision_thinking_type, 'disabled');
  assert.equal(body.planner_reasoning_effort, 'low');
  assert.equal(body.planner_max_output_tokens, '8192');
  assert.equal(body.planner_timeout_seconds, '120');
  assert.equal(body.voice_config_file, 'C:\\models\\voice.json');
  assert.equal(Object.keys(body).length, 19);
  assert.match(h.text(), /当前制作结束后重启服务生效/);
  h.unmount();
});

test('pasting a key instead of a credential path never submits it', async () => {
  const calls = [];
  const request = async (path, options) => { calls.push({ path, options }); return {}; };
  const h = componentHarness(file, 'RuntimeSettingsPanel', { request });
  await h.flush();
  await h.change('规划凭据文件路径', 'not-a-file-path');
  await h.submit('保存运行配置');
  assert.equal(calls.filter((call) => call.options?.method === 'PUT').length, 0);
  assert.match(h.text(), /不要粘贴密钥/);
  h.unmount();
});
