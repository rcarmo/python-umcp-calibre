import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from plugins.calibre_umcp_plugin.bridge import BridgeMethodError, CalibreRpcBridge
from plugins.calibre_umcp_plugin.mcp import CalibrePluginMCPServer
from test_plugin_bridge import FakeGui


class NativeTasksBridgeTests(unittest.TestCase):
    def test_capture_mcp_log_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            gui = FakeGui()
            job = SimpleNamespace(description='Email Economist to rui@example.com',
                                  name='email', id=1, start_time=time.time(), duration=2.,
                                  is_finished=True, failed=False, killed=False,
                                  percent=100, log=io.StringIO('Accepted\nsend: AUTH secret\n'))
            gui.job_manager = SimpleNamespace(jobs=[job])
            path = str(Path(root) / 'tasks.sqlite')
            bridge = CalibreRpcBridge(gui, task_history_path=path)
            with patch('plugins.calibre_umcp_plugin.mcp.CalibreRpcBridge', return_value=bridge):
                server = CalibrePluginMCPServer(gui)
            try:
                page = server.tool_list_native_tasks_readonly()
                self.assertEqual(page['total'], 1)
                task = page['items'][0]
                self.assertEqual(task['status'], 'completed')
                self.assertFalse(task['delivery_confirmed'])
                self.assertNotIn('rui@example.com', task['description'])
                log = server.tool_get_native_task_log_readonly(task['task_id'])
                self.assertIn('Accepted', log['text'])
                self.assertNotIn('AUTH secret', log['text'])
                self.assertEqual(server.tool_list_bridge_jobs_readonly(), {'items': []})
                with self.assertRaises(ValueError):
                    server.tool_list_native_tasks_readonly(limit=201)
                with self.assertRaises(BridgeMethodError):
                    bridge.call_serialized('get_native_task', {'task_id': task['task_id'] + '0'})
            finally:
                bridge.close()
            gui.job_manager.jobs = []
            restored = CalibreRpcBridge(gui, task_history_path=path)
            try:
                self.assertEqual(restored.call_serialized('list_native_tasks', {})['total'], 1)
            finally:
                restored.close()

    def test_smtp_config_secrets_and_cache_invalidation(self):
        import base64
        smtp = SimpleNamespace(config=lambda: SimpleNamespace(parse=lambda: SimpleNamespace(
            relay_password='first-password'.encode().hex(), relay_username='smtp-user', relay_host='smtp.private.local')))
        with patch.dict('sys.modules', {'calibre.utils.smtp': smtp}):
            gui = FakeGui()
            job = SimpleNamespace(description='first-password smtp-user smtp.private.local',
                                  details='first-password ' + base64.b64encode(b'first-password').decode())
            gui.job_manager = SimpleNamespace(jobs=[job])
            bridge = CalibreRpcBridge(gui)
            try:
                bridge.capture_native_tasks()
                text = str(bridge.task_history.list()) + str(bridge.task_history.log(bridge.task_history.list()['items'][0]['task_id']))
                for secret in ('first-password', 'smtp-user', 'smtp.private.local', base64.b64encode(b'first-password').decode()):
                    self.assertNotIn(secret, text)
                # Configuration changes invalidate cached redaction without a job-text change.
                job.description = 'second-password'
                bridge.capture_native_tasks()
                smtp.config = lambda: SimpleNamespace(parse=lambda: SimpleNamespace(relay_password=b'second-password'.hex()))
                bridge.capture_native_tasks()
                self.assertNotIn('second-password', str(bridge.task_history.list()))
            finally:
                bridge.close()

    def test_does_not_recapture_old_completed_gui_jobs(self):
        gui = FakeGui()
        gui.job_manager = SimpleNamespace(jobs=[SimpleNamespace(start_time=time.time() - 31 * 86400,
                                                              duration=1, description='Old issue')])
        bridge = CalibreRpcBridge(gui)
        try:
            self.assertEqual(bridge.call_serialized('list_native_tasks', {})['total'], 0)
        finally:
            bridge.close()


if __name__ == '__main__':
    unittest.main()
