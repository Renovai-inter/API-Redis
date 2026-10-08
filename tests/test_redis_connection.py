import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import MagicMock, patch

import redis


class RedisConnectionTest(unittest.TestCase):
    def test_health_reports_redis_unavailable(self):
        with patch('redis_connection.conectar_redis', return_value=None), \
             patch('dotenv.load_dotenv'), patch('threading.Thread'), \
             patch.dict('sys.modules', sincronizador=MagicMock()):
            module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'))
        app = module['app']
        with app.test_client() as client:
            self.assertEqual(client.get('/health').status_code, 503)
            connected = MagicMock()
            module['health'].__globals__['r'] = connected
            self.assertEqual(client.get('/health').status_code, 200)
            connected.ping.side_effect = redis.ConnectionError('unavailable')
            self.assertEqual(client.get('/health').status_code, 503)

    def test_synchronizer_uses_shared_connection(self):
        with patch('dotenv.load_dotenv'):
            module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'sincronizador.py'))
        cls = module['SincronizadorRotasPostgreSQL']
        with patch('redis_connection.conectar_redis') as shared:
            cls._conectar_redis.__globals__['conectar_redis'] = shared
            sync = cls.__new__(cls)
            sync._conectar_redis()
            self.assertIs(sync.redis_client, shared.return_value)
            shared.return_value = None
            with self.assertRaises(redis.ConnectionError):
                sync._conectar_redis()

    def test_connection_selection(self):
        for scenario in ('url', 'missing', 'empty', 'invalid', 'unreachable', 'both_fail'):
            with self.subTest(scenario=scenario):
                remote, local = MagicMock(), MagicMock()
                env = {} if scenario == 'missing' else {'REDIS_URL': '' if scenario == 'empty' else 'redis://example:6379'}
                if scenario in ('unreachable', 'both_fail'):
                    remote.ping.side_effect = redis.ConnectionError('unavailable')
                if scenario == 'both_fail':
                    local.ping.side_effect = redis.ConnectionError('unavailable')
                with patch.dict(os.environ, env, clear=True), \
                     patch('redis.Redis.from_url', return_value=remote) as from_url, \
                     patch('redis.Redis', return_value=local) as constructor, \
                     patch('dotenv.load_dotenv'), \
                     patch('threading.Thread') as thread, \
                     patch.dict('sys.modules', sincronizador=MagicMock()):
                    # Patch the class method on the mocked constructor used by main.
                    constructor.from_url = from_url
                    if scenario == 'invalid':
                        from_url.side_effect = ValueError('invalid URL')
                    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'))
                    self.assertEqual(module['intervalo_sincronizacao'], 300)
                    thread.assert_called_once_with(target=module['loop_sincronizacao'], daemon=True)
                    expected = remote if scenario == 'url' else None if scenario == 'both_fail' else local
                    self.assertIs(module['r'], expected)
                    self.assertEqual(from_url.call_count, 0 if scenario in ('missing', 'empty') else 1)
                    if scenario == 'url':
                        constructor.assert_not_called()
                    else:
                        constructor.assert_called_once_with(
                            host='localhost', port=6379, password=None,
                            protocol=2, decode_responses=True, socket_connect_timeout=5, socket_timeout=5,
                        )
                    if scenario in ('unreachable', 'both_fail'):
                        remote.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
