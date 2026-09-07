import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

from fastapi import HTTPException
from fastapi.testclient import TestClient
import main
import safe_http
from make_openers import to_score


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.object(main, 'API_KEY', 'test-secret')
        self.key.start()
        self.addCleanup(self.key.stop)
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected network call'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.client = TestClient(main.app)
        self.headers = {'x-api-key': 'test-secret'}

    def test_auth_all_protected_routes(self):
        for route in ('check-domain', 'personalize-opener', 'classify-reply', 'handle-reply'):
            for key in (None, '', ' '):
                with self.subTest(route=route, key=key), patch.object(main, 'API_KEY', key):
                    self.assertEqual(self.client.post('/'+route, headers=self.headers).status_code, 503)
            self.assertEqual(self.client.post('/'+route).status_code, 401)
            self.assertEqual(self.client.post('/'+route, headers={'x-api-key': 'wrong'}).status_code, 401)
        self.assertEqual(self.client.get('/health').status_code, 200)

    def test_bad_bodies(self):
        with patch.object(main, 'ANTHROPIC_API_KEY', 'fake'):
            for route in ('check-domain', 'personalize-opener', 'classify-reply', 'handle-reply'):
                for body in (['unexpected'], 123, None):
                    with self.subTest(route=route, body=body):
                        import json
                        self.assertEqual(self.client.post('/'+route, headers=self.headers, content=json.dumps(body)).status_code, 422)
                self.assertEqual(self.client.post('/'+route, headers=self.headers, content='{').status_code, 422)

    def test_domain_types_and_paths(self):
        for domain in (123, True, [], {}, ' ', 'https://example.com/path', 'file://example.com', 'user:pass@example.com', 'example.com:22'):
            with self.subTest(domain=domain):
                self.assertEqual(self.client.post('/check-domain', headers=self.headers, json={'domain':domain}).status_code, 422)

    def test_query_and_body_supported(self):
        result = (SimpleNamespace(status_code=200, url='https://example.com'), 1)
        with patch.object(main, 'probe', return_value=result):
            for kwargs in ({'json':{'domain':'example.com'}}, {'params':{'domain':'example.com'}}):
                self.assertTrue(self.client.post('/check-domain', headers=self.headers, **kwargs).json()['is_live'])

    def test_classifier_contract(self):
        for label in main.REPLY_LABELS:
            result = main.parse_classification(label+': explanation')
            self.assertEqual(result['classification'], label)
            self.assertFalse(result['parse_error'])
        for raw in ('LABEL: This is not interested.', 'Allowed labels: not_interested, interested. My answer is interested.', 'interested', 'interested:', 'unknown: interested'):
            result = main.parse_classification(raw)
            self.assertEqual(result['classification'], 'needs_info')
            self.assertTrue(result['parse_error'])
            self.assertEqual(result['suggested_action'], 'flag_for_manual_review')

    def test_classifier_endpoint_with_fake_llm(self):
        response = Mock()
        response.json.return_value = {'content':[{'text':'LABEL: This is not interested.'}]}
        with patch.object(main,'ANTHROPIC_API_KEY','fake'), patch.object(main.requests,'post',return_value=response):
            result = self.client.post('/classify-reply',headers=self.headers,json={'reply_text':'No thanks'}).json()
        self.assertTrue(result['parse_error'])
        self.assertEqual(result['classification'],'needs_info')

    def test_quality_gate_returns_flagged_text(self):
        response = Mock()
        response.json.return_value = {'content':[{'text':'Try [specific use case] for your software.'}]}
        with patch.object(main,'ANTHROPIC_API_KEY','fake'), patch.object(main.requests,'post',return_value=response):
            result = self.client.post('/personalize-opener',headers=self.headers,json={'company_name':'Example','context':'Builds software'}).json()
        self.assertFalse(result['passed_quality_check'])
        self.assertIn('[specific use case]',result['opener'])

    def test_score(self):
        for raw, expected in [('5',5),('Fit score (1-5): 4 - Outbound fit',4),('Fit score (1–5): 3',3),('11 employees',0),('6',0),('',0)]:
            self.assertEqual(to_score(raw),expected)

    def test_reply_handler_preserves_routing(self):
        with patch.object(main,'find_hubspot_contact',return_value=None):
            result = self.client.post('/handle-reply',headers=self.headers,json={'from':'Name <person@example.com>','reply_text':'Hi'}).json()
            self.assertEqual(result['action'],'ignore')
        contact = {'id':'123','properties':{'company':'Example'}}
        fallback = main.parse_classification('LABEL: not interested')
        with patch.object(main,'find_hubspot_contact',return_value=contact), patch.object(main,'classify_reply_text',return_value=fallback):
            result = self.client.post('/handle-reply',headers=self.headers,json={'from':'person@example.com','reply_text':'No'}).json()
            self.assertEqual(result['action'],'needs_info')
            self.assertTrue(result['parse_error'])
            self.assertEqual(result['hubspot_contact_id'],'123')


class PublicHTTPTests(unittest.TestCase):
    def addresses(self, *ips):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip,443)) for ip in ips]

    def test_reject_nonpublic_dns(self):
        for ip in ('127.0.0.1','10.0.0.1','169.254.169.254','::1','::ffff:127.0.0.1','224.0.0.1'):
            with self.subTest(ip=ip), patch.object(socket,'getaddrinfo',return_value=self.addresses(ip)):
                with self.assertRaises(HTTPException):
                    safe_http.public_addresses('example.com',443)
        with patch.object(socket,'getaddrinfo',return_value=self.addresses('8.8.8.8','10.0.0.1')):
            with self.assertRaises(HTTPException):
                safe_http.public_addresses('example.com',443)

    def test_pinned_ip_and_tls_hostname(self):
        response = Mock(status=200, headers={})
        pool = Mock()
        pool.urlopen.return_value = response
        with patch.object(socket,'getaddrinfo',return_value=self.addresses('8.8.8.8')), patch.object(safe_http.urllib3,'HTTPSConnectionPool',return_value=pool) as constructor:
            self.assertEqual(safe_http.public_request('https://example.com').status_code,200)
        self.assertEqual(constructor.call_args.args[0],'8.8.8.8')
        self.assertEqual(constructor.call_args.kwargs['server_hostname'],'example.com')
        self.assertEqual(constructor.call_args.kwargs['assert_hostname'],'example.com')
        self.assertEqual(pool.urlopen.call_args.kwargs['headers']['Host'],'example.com')
        self.assertFalse(pool.urlopen.call_args.kwargs['redirect'])
        self.assertFalse(pool.urlopen.call_args.kwargs['preload_content'])
        response.close.assert_called_once()
        pool.close.assert_called_once()

    def test_private_redirect_blocked(self):
        pool = Mock()
        pool.urlopen.return_value = Mock(status=302, headers={'Location':'http://127.0.0.1/'})
        with patch.object(socket,'getaddrinfo',side_effect=[self.addresses('8.8.8.8'),self.addresses('127.0.0.1')]), patch.object(safe_http.urllib3,'HTTPSConnectionPool',return_value=pool), patch.object(safe_http.urllib3,'HTTPConnectionPool') as http:
            with self.assertRaises(HTTPException):
                safe_http.public_request('https://example.com')
            http.assert_not_called()

    def test_head_405_falls_back_to_get(self):
        result = SimpleNamespace(status_code=200,url='https://example.com/')
        with patch.object(main,'public_request',side_effect=[SimpleNamespace(status_code=405,url=result.url),result]) as request:
            main.probe('https://example.com')
            self.assertEqual(request.call_args.kwargs['method'],'GET')

    def test_redirect_loop_bounded(self):
        pool = Mock()
        pool.urlopen.return_value = Mock(status=302, headers={'Location':'/again'})
        with patch.object(socket,'getaddrinfo',return_value=self.addresses('8.8.8.8')), patch.object(safe_http.urllib3,'HTTPSConnectionPool',return_value=pool):
            with self.assertRaises(safe_http.requests.exceptions.TooManyRedirects):
                safe_http.public_request('https://example.com')
            self.assertEqual(pool.urlopen.call_count,6)


if __name__ == '__main__':
    unittest.main()
