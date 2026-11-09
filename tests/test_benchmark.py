"""Offline regressions for portable data, terminal scoring, images and execution."""
import asyncio
import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from medenv.cli import build_parser, run_configuration
from medenv.data import SUBSETS, prepare_med_data
from medenv.evaluation import evaluate_doctor_results
from medenv.serialization import redact_secrets
from rllm.agents.agent import Step, Trajectory
from rllm.agents.med_agent import MedicalAgent
from rllm.engine.agent_execution_engine import AgentExecutionEngine
from rllm.engine.rollout.openai_engine import OpenAIEngine
from rllm.engine.rollout.rollout_engine import ModelOutput
from rllm.environments.medgym.medgym_env import MedicalDialogueEnv
from rllm.parser import ChatTemplateParser
from rllm.parser.tool_parser import QwenToolParser
from rllm.rewards.med_diagnosis_reward import med_diagnosis_reward
from rllm.tools.med_tool.cxr_tool import CXRTool
from rllm.tools.med_tool.exam_result_tool import ExamResultTool
from rllm.tools.tool_base import Tool, ToolOutput
from scripts.validate_data import validate

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data/synthetic'
FINAL = r'The final diagnosis is: \boxed{Synthetic condition A}.'


def call(name, arguments):
    return [{'id': 'test_call', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]


class ContextProbe(Tool):
    def __init__(self, name='request_exam'):
        super().__init__(name=name, description='Synthetic context probe')

    @property
    def json(self):
        return {'type': 'function', 'function': {'name': self.name, 'description': 'Synthetic context probe', 'parameters': {'type': 'object', 'properties': {}}}}

    def forward(self, context=None, **kwargs):
        return ToolOutput(name=self.name, output={'has_ehr': bool(context and context.get('ehr'))})


class TinyTokenizer:
    name_or_path = 'synthetic-qwen'
    eos_token = '<|im_end|>'
    bos_token = '<|im_start|>'

    def apply_chat_template(self, messages, add_generation_prompt=False, **kwargs):
        return ''.join(f'<|im_start|>{m["role"]}\n{m["content"]}<|im_end|>\n' for m in messages) + ('<|im_start|>assistant\n' if add_generation_prompt else '')

    def encode(self, text, **kwargs):
        return list(text.encode())

    def decode(self, ids, **kwargs):
        return bytes(ids).decode()


class ScriptedEngine(AgentExecutionEngine):
    async def get_model_response(self, prompt, application_id, **kwargs):
        tool_seen = any(message['role'] == 'tool' for message in prompt)
        name, arguments = ('diagnosis', {'final_response': FINAL}) if tool_seen else ('cxr', {})
        response = '<tool_call>\n' + json.dumps({'name': name, 'arguments': arguments}) + '\n</tool_call>'
        return ModelOutput(text=response)


class BudgetAwareEngine(AgentExecutionEngine):
    async def get_model_response(self, prompt, application_id, **kwargs):
        last_message = prompt[-1]
        if last_message['role'] == 'user' and 'submit your final diagnosis now' in last_message.get('content', ''):
            return ModelOutput(text='<tool_call>' + json.dumps({'name': 'diagnosis', 'arguments': {'final_response': FINAL}}) + '</tool_call>')
        return ModelOutput(text='<tool_call>{"name": "cxr", "arguments": {}}</tool_call>')


class BenchmarkTests(unittest.TestCase):
    def test_last_turn_requests_diagnosis_before_environment_closes(self):
        tasks, _, _ = prepare_med_data(str(DATA))
        with tempfile.TemporaryDirectory() as output, patch.dict(os.environ, {'MEDENV_TRAJECTORY_DIR': output, 'MEDENV_CXR_MODE': 'reports'}):
            engine = BudgetAwareEngine(
                tokenizer=TinyTokenizer(), agent_class=MedicalAgent,
                agent_args={'tools': ['cxr', 'diagnosis']},
                env_class=MedicalDialogueEnv,
                env_args={'tools': ['cxr', 'diagnosis'], 'reward_fn': med_diagnosis_reward, 'max_steps': 2, 'timeout': 30},
                n_parallel_agents=1, max_steps=2, max_prompt_length=20000, max_response_length=20000,
                rollout_engine_args={'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'None'},
            )
            try:
                results = asyncio.run(engine.execute_tasks(tasks))
                metrics = evaluate_doctor_results(results, tasks)
                self.assertEqual(metrics['summary']['scored_runs'], 1)
                self.assertEqual(metrics['summary']['correct_runs'], 1)
                self.assertEqual(len(results[0].steps), 2)
                self.assertEqual(results[0].steps[-1].action[0]['function']['name'], 'diagnosis')
                self.assertIn('final_turn_instruction', results[0].steps[-1].info)
            finally:
                engine.shutdown()

    def test_parameterless_tool_call_without_arguments(self):
        response = '<tool_call>{"name": "cxr"}</tool_call>'
        calls = QwenToolParser().parse(response)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, 'cxr')
        self.assertEqual(calls[0].arguments, {})
        parser = ChatTemplateParser.get_parser(TinyTokenizer(), disable_thinking=True)
        parsed = parser.parse_completion(TinyTokenizer().encode(response))
        self.assertEqual(parsed['tool_calls'][0].arguments, {})

    def test_exam_does_not_generate_from_empty_knowledge(self):
        model = Mock()
        model.chat.return_value = 'NONE'
        tool = ExamResultTool()
        with patch.object(tool, '_get_llm', return_value=model):
            result = tool.forward(exam_type='blood tests', context={'ehr': {}, 'knowbase': []})
        self.assertIsNone(result.error)
        self.assertEqual(result.output['source'], 'none')
        self.assertEqual(model.chat.call_count, 1)

    def test_chat_template_requiring_a_user_turn(self):
        class UserRequiredTokenizer(TinyTokenizer):
            def apply_chat_template(self, messages, **kwargs):
                if not any(message['role'] == 'user' for message in messages):
                    raise ValueError('No user query found in messages')
                return super().apply_chat_template(messages, **kwargs)

        parser = ChatTemplateParser.get_parser(UserRequiredTokenizer())
        self.assertIn('assistant', parser.generation_prompt)

    def test_all_subsets_and_repeats(self):
        stats, errors = validate(DATA)
        self.assertEqual(errors, [])
        self.assertEqual(sum(item['cases'] for item in stats.values()), 4)
        for subset in SUBSETS:
            tasks, cases, _ = prepare_med_data(str(DATA), subdir=subset, repeat_k=3)
            self.assertEqual(len(cases), 1)
            self.assertEqual([task['_repeat_id'] for task in tasks], [0, 1, 2])
            for study in cases[0]['ehr']['CXR']:
                for dicom in study['dicoms']:
                    self.assertTrue(Path(dicom['jpg_path_abs']).is_file())

    def test_missing_data_and_bad_repeat_fail(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(FileNotFoundError):
                prepare_med_data(empty)
        with self.assertRaises(ValueError):
            prepare_med_data(str(DATA), repeat_k=0)

    def test_subset_controls_tool_set(self):
        args = build_parser().parse_args(['--subdir', 'without_img/ed_hosp'])
        _, no_cxr, names = run_configuration(args)
        self.assertTrue(no_cxr)
        self.assertIn('request_exam', names)
        self.assertNotIn('cxr', names)
        args = build_parser().parse_args(['--no_cxr', '--subdir', 'with_img/hosp_only'])
        with self.assertRaises(ValueError):
            run_configuration(args)

    def test_cxr_resolves_relative_image_and_modes(self):
        tasks, _, _ = prepare_med_data(str(DATA))
        tool = CXRTool()
        with patch.dict(os.environ, {'MEDENV_CXR_MODE': 'vision'}):
            result = tool.forward(context=tasks[0]['context'])
        image = result.output['images'][0]
        self.assertNotIn('image_error', image)
        self.assertTrue(base64.b64decode(image['image_base64']).startswith(b'\x89PNG'))
        self.assertEqual(image['mime'], 'image/png')
        with patch.dict(os.environ, {'MEDENV_CXR_MODE': 'reports'}):
            report = tool.forward(context=tasks[0]['context'])
        self.assertNotIn('images', report.output)
        self.assertIn('Synthetic', report.output['report_text'])

    def test_diagnosis_finalizes_and_scores_before_step_limit(self):
        tasks, _, _ = prepare_med_data(str(DATA), subdir='without_img/hosp_only')
        env = MedicalDialogueEnv(task=tasks[0], tools=['diagnosis'], reward_fn=med_diagnosis_reward, timeout=30)
        try:
            env.reset()
            _, reward, done, info = env.step(call('diagnosis', {'final_response': FINAL}))
            self.assertTrue(done)
            self.assertEqual(reward, 1.0)
            self.assertTrue(info['is_correct'])
        finally:
            env.close()

    def test_exam_receives_case_context_without_images(self):
        tasks, _, _ = prepare_med_data(str(DATA), subdir='without_img/hosp_only')
        env = MedicalDialogueEnv(task=tasks[0], tool_map={'request_exam': ContextProbe}, timeout=30)
        try:
            env.reset()
            observation, _, done, _ = env.step(call('request_exam', {}))
            self.assertFalse(done)
            self.assertTrue(json.loads(observation['tool_outputs']['test_call'])['has_ehr'])
        finally:
            env.close()

    def test_metrics_do_not_read_hidden_gold_as_prediction(self):
        result = Trajectory(task={'ground_truth': 'Synthetic condition A'}, steps=[Step(action='unrelated answer')])
        metrics = evaluate_doctor_results([result], [{'case_id': 'test', 'ground_truth': 'Synthetic condition A'}])
        self.assertEqual(metrics['summary']['correct_runs'], 0)
        self.assertEqual(metrics['summary']['unscored_runs'], 1)
        self.assertEqual(metrics['rows'][0]['prediction'], 'unrelated answer')

    def test_credentials_redacted_recursively(self):
        result = redact_secrets({'task': {'judge_api_key': 'private-value'}, 'items': [{'api_key': 'private-value'}]})
        self.assertNotIn('private-value', json.dumps(result))

    def test_real_engine_multiturn_spawn_and_serialization(self):
        tasks, _, _ = prepare_med_data(str(DATA), repeat_k=2)
        for task in tasks:
            task['unused_api_key'] = 'do-not-persist-this'
        with tempfile.TemporaryDirectory() as output, patch.dict(os.environ, {'MEDENV_TRAJECTORY_DIR': output, 'MEDENV_CXR_MODE': 'reports'}):
            engine = ScriptedEngine(
                tokenizer=TinyTokenizer(), agent_class=MedicalAgent,
                agent_args={'tools': ['cxr', 'diagnosis']},
                env_class=MedicalDialogueEnv,
                env_args={'tools': ['cxr', 'diagnosis'], 'reward_fn': med_diagnosis_reward, 'timeout': 30},
                n_parallel_agents=2, max_steps=4, max_prompt_length=20000, max_response_length=20000,
                rollout_engine_args={'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'None'},
            )
            try:
                results = asyncio.run(engine.execute_tasks(tasks))
                metrics = evaluate_doctor_results(results, tasks)
                self.assertEqual(metrics['summary']['correct_runs'], 2)
                self.assertEqual([len(result.steps) for result in results], [2, 2])
                files = list(Path(output).glob('trajectory_*.json'))
                self.assertEqual(len(files), 2)
                for path in files:
                    self.assertNotIn('do-not-persist-this', path.read_text())
            finally:
                engine.shutdown()


class ApiTests(unittest.TestCase):
    def setUp(self):
        proxy_patch = patch.dict(os.environ, {'NO_PROXY': 'localhost,127.0.0.1,::1', 'no_proxy': 'localhost,127.0.0.1,::1'})
        proxy_patch.start()
        self.addCleanup(proxy_patch.stop)
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, body))
                message = 'YES' if body['model'] == 'judge_agent' else FINAL
                choice = {'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': message}}
                if self.path.endswith('/completions') and not self.path.endswith('/chat/completions'):
                    choice = {'index': 0, 'finish_reason': 'stop', 'text': FINAL}
                payload = {'id': 'synthetic', 'created': 0, 'object': 'chat.completion', 'model': body['model'],
                           'choices': [choice], 'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_judge_http_adapter(self):
        result = med_diagnosis_reward({'ground_truth': 'Synthetic condition A', 'judge_model_name': 'judge_agent', 'judge_base_url': self.url}, FINAL)
        self.assertTrue(result.is_correct)
        self.assertEqual(result.metadata['result_reward_type'], 'judge')

    def test_vision_http_preserves_image_and_model(self):
        url = 'data:image/png;base64,synthetic'
        engine = OpenAIEngine(tokenizer=TinyTokenizer(), base_url=self.url, api_key='None', use_chat_completions=True)
        async def run():
            try:
                return await engine.get_model_response([
                    {'role': 'tool', 'content': 'synthetic result', 'tool_call_id': 'fake'},
                    {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': url}}]},
                ], model='doctor_agent')
            finally:
                await engine.client.close()
        output = asyncio.run(run())
        self.assertEqual(output.text, FINAL)
        endpoint, body = self.requests[-1]
        self.assertEqual(endpoint, '/v1/chat/completions')
        self.assertEqual(body['model'], 'doctor_agent')
        self.assertEqual(body['messages'][1]['content'][0]['image_url']['url'], url)
        self.assertEqual(body['messages'][0]['role'], 'user')


if __name__ == '__main__':
    unittest.main()
