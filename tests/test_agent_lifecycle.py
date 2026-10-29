import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import without creating report folders in the user's profile.
with patch.object(Path, 'mkdir'):
    import web_app as web
    import client_agent as agent


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        web._client_register_sent.clear()
        web._client_files.clear()
        web._agents.clear()

    def upload(self):
        data = {'dsr_no': '1', 'serial_no': '2', 'pf_no': '3', 'target_ip': '127.0.0.1'}
        return self.client.post('/api/client_upload', query_string={'data': json.dumps(data)},
            headers={'X-Token': web.SECRET_TOKEN, 'X-Agent-ID': 'test'},
            data={'pdf_file': (io.BytesIO(b'%PDF-test'), 'report.pdf'),
                  'json_file': (io.BytesIO(b'{}'), 'report.json')})

    def test_save_success_survives_agent_shutdown(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(web, 'OUTPUT_DIR', Path(folder)), \
             patch.object(web, '_client_upload_forward_to_device_scan', return_value=(201, {'success': True}, None)):
            self.assertEqual(self.upload().status_code, 201)
            with patch.object(web, '_call_agent', side_effect=AssertionError('Stopped agent must not be called')):
                self.assertTrue(self.client.get('/api/client_status/test').json['database_saved'])
            self.assertTrue(self.client.get('/api/client_result/test').json['ok'])

    def test_save_failure_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(web, 'OUTPUT_DIR', Path(folder)), \
             patch.object(web, '_client_upload_forward_to_device_scan', return_value=(502, {}, 'Save failed')):
            self.assertEqual(self.upload().status_code, 502)
            self.assertNotIn('test', web._client_register_sent)

    def test_failed_start_stop_preserves_registration(self):
        web._agents['test'] = {'ip': '127.0.0.1'}
        with patch.object(web, '_call_agent', return_value={'error': 'unreachable'}):
            for action in ('start', 'stop'):
                self.assertEqual(self.client.post(f'/api/{action}_agent/test').status_code, 502)
                self.assertIn('test', web._agents)

    def test_missing_control_endpoint_offers_upgrade(self):
        from urllib.error import HTTPError
        web._agents['test'] = {'ip': '127.0.0.1', 'port': 7979}
        for action in ('start', 'stop'):
            error = HTTPError('http://localhost', 404, 'Not Found', {}, io.BytesIO(b'{}'))
            with patch('urllib.request.urlopen', side_effect=error):
                response = self.client.post(f'/api/{action}_agent/test')
            self.assertEqual(response.status_code, 502)
            self.assertFalse(response.json['_transient'])
            self.assertTrue(response.json['upgrade_required'])
            self.assertEqual(response.json['installer_url'], '/install/windows')
            self.assertIn('test', web._agents)

    def test_missing_status_endpoint_does_not_poll_forever(self):
        web._agents['test'] = {'ip': '127.0.0.1'}
        with patch.object(web, '_call_agent', return_value={
                'error': 'Missing status endpoint', 'http_status': 404, '_transient': False}):
            response = self.client.get('/api/client_status/test')
        self.assertEqual(response.json['status'], 'error')

    def test_empty_error_does_not_fail_running_scan(self):
        for status in ('running', 'uploading'):
            with patch.object(web, '_call_agent', return_value={
                    'status': status, 'error': '', 'log': ['Working'], 'total_lines': 1}):
                response = self.client.get('/api/client_status/test')
            self.assertEqual(response.json['status'], status)
            self.assertEqual(response.json['lines'], ['Working'])

    def test_scan_failure_preserves_message_and_log(self):
        with patch.object(web, '_call_agent', return_value={
                'status': 'error', 'error': 'Database unavailable',
                'log': ['Upload failed'], 'total_lines': 1}):
            response = self.client.get('/api/client_status/test')
        self.assertEqual(response.json['error'], 'Database unavailable')
        self.assertEqual(response.json['lines'], ['Upload failed'])

    def test_refused_backend_connection_explains_save_failure(self):
        from urllib.error import URLError
        with patch('urllib.request.urlopen', side_effect=URLError('Connection refused')):
            status, _, error = web._client_upload_forward_to_device_scan({}, [])
        self.assertEqual(status, 502)
        self.assertIn('DEVICE_SCAN_API_URL', error)
        self.assertIn('could not be saved', error)

    def test_upload_preserves_server_error_after_retries(self):
        from urllib.error import HTTPError
        def refused(*args, **kwargs):
            raise HTTPError('http://localhost', 502, 'Bad Gateway', {},
                            io.BytesIO(b'{"error":"Database API is unreachable"}'))
        with patch.object(agent, '_build_results_multipart', return_value=(b'data', 'boundary')), \
             patch('urllib.request.urlopen', side_effect=refused), patch.object(agent.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Database API is unreachable'):
                agent._upload_results('a.pdf', 'a.json', '127.0.0.1', 'test', {}, lambda _: None)

    def test_installer_and_current_deployer(self):
        response = self.client.get('/install/windows', base_url='http://scanner.test:5001')
        self.assertIn('itc_install.bat', response.headers['Content-Disposition'])
        self.assertIn(b'http://scanner.test:5001', response.data)
        self.assertNotIn(b'client_agent.exe', response.data)
        with self.client.get('/static/deploy_client.py') as deployed:
            self.assertIn(b'def install_windows_launcher', deployed.data)

    def test_upload_exhaustion_raises(self):
        with patch.object(agent, '_build_results_multipart', return_value=(b'data', 'boundary')), \
             patch('urllib.request.urlopen', side_effect=OSError('offline')), patch.object(agent.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Database save failed'):
                agent._upload_results('a.pdf', 'a.json', '127.0.0.1', 'test', {}, lambda _: None)

    def test_upstream_application_error_is_not_success(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"success": false}'
        response.headers = {'Content-Type': 'application/json'}
        response.getcode.return_value = 200
        with patch('urllib.request.urlopen', return_value=response):
            status, _, error = web._client_upload_forward_to_device_scan({}, [])
        self.assertEqual(status, 502)
        self.assertIsNotNone(error)


if __name__ == '__main__':
    unittest.main()
