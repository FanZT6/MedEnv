"""Service API schema tests that do not load model weights."""
import importlib.util
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('fastapi'), 'Install FastAPI to test the optional grounding API')
class GroundingServiceTests(unittest.TestCase):
    def test_request_body_is_a_model_and_post_works(self):
        from fastapi.testclient import TestClient
        from services import grounding_server
        app = grounding_server.create_app('unused-local-model', 'cpu')
        with TestClient(app) as client, patch.object(grounding_server, 'run_grounding', return_value={'detections': []}):
            self.assertEqual(client.get('/health').status_code, 200)
            response = client.post('/ground', json={'image_base64': 'synthetic', 'text_prompts': ['test region']})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['detections'], [])
            self.assertEqual(client.post('/ground', json={}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
