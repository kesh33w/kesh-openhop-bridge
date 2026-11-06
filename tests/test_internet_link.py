import asyncio
import copy
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from repeater.web.internet_link import candidate, view, update
from repeater.config_manager import ConfigManager

@pytest.fixture
def config():
    return {'radios': [{'id': 'host-rf'}], 'private_island': {
        'enabled': True, 'node_id': 'host', 'outbound': {'host-rf': ['device-a']},
        'inbound': [{'peer': 'device-a', 'radio': 'remote-rf', 'local_radio': 'host-rf'}],
        'transport': {'enabled': True, 'port': 7443, 'private_key_file': '/secret/key',
                      'peer_fingerprints': {'device-a': 'a'*64}}}}

@pytest.mark.parametrize('updates', [None, {}, {'enabled': False}, {'transport': {}}, {'send_timeout': True},
    {'send_timeout': 0}, {'send_timeout': float('nan')}, {'send_timeout': 6}, {'max_inflight': 1.5},
    {'max_inflight': False}, {'max_rx_tasks': 65}, {'max_rx_tasks': 0}])
def test_rejects_unapproved_fields_and_invalid_limits(config, updates):
    before = copy.deepcopy(config)
    with pytest.raises(ValueError): candidate(config, updates)
    assert config == before

def rig(config, tmp_path):
    _, parsed, limit = candidate(config, {'max_rx_tasks': 16})
    service = NS(port=7443, _server=NS(is_serving=lambda: True), transports={'device-a': NS(connected=False)},
                 enrollment=object(), failures=0, settings={'secret': 'HIDDEN'})
    binding = NS(service=service, adapter=NS(config=parsed), limit=limit, closed=False, peer_pending=0, dropped=0)
    api = NS(config=config, daemon_instance=NS(private_island=binding))
    api.config_manager = ConfigManager(str(tmp_path/'config.yaml'), config)
    return api, binding

def test_live_save_persists_and_retains_registry_routes_and_secrets(config, tmp_path):
    api, binding = rig(config, tmp_path)
    before = copy.deepcopy(config['private_island'])
    assert asyncio.run(update(api, {'send_timeout': 2.5, 'max_inflight': 8, 'max_rx_tasks': 20}))
    assert binding.adapter.config.send_timeout == 2.5 and binding.limit == 20
    import yaml
    saved = yaml.safe_load((tmp_path/'config.yaml').read_text())
    for key in ('transport', 'inbound', 'outbound', 'node_id', 'enabled'):
        assert saved['private_island'][key] == before[key]
    result = view(config, binding)
    assert result['settings'] == result['active_settings']
    assert not any(secret in str(result) for secret in ('HIDDEN', '/secret/key', 'a'*64, 'private_key_file', 'peer_fingerprints'))
    assert result['peers'] == [{'id': 'device-a', 'connected': False,
                               'last_valid_inbound_frame_age_seconds': None}]
    binding.service.transports['device-a'].connected = True
    assert view(config, binding)['peers'][0]['connected']

def test_peer_activity_status_keeps_age_separate_from_connection(config, tmp_path):
    from private_link.transport import PeerStream
    from test_transport import PIN, MockTls, Writer

    _, binding = rig(config, tmp_path)
    tick = [10.0]
    transport = PeerStream('device-a', PIN, clock=lambda: tick[0])
    binding.service.transports['device-a'] = transport
    writer = Writer(MockTls())
    assert transport.attach(writer)
    assert view(config, binding)['peers'][0] == {
        'id': 'device-a', 'connected': True, 'last_valid_inbound_frame_age_seconds': None,
    }
    transport._record_valid_inbound_frame(writer, tick[0])
    tick[0] = 15.5
    assert view(config, binding)['peers'][0]['last_valid_inbound_frame_age_seconds'] == 5.5
    transport.detach(writer)
    assert view(config, binding)['peers'][0] == {
        'id': 'device-a', 'connected': False, 'last_valid_inbound_frame_age_seconds': 5.5,
    }
    tick[0] = 20.0
    replacement = Writer(MockTls())
    assert transport.attach(replacement)
    assert view(config, binding)['peers'][0] == {
        'id': 'device-a', 'connected': True, 'last_valid_inbound_frame_age_seconds': 10.0,
    }
    transport.detach(replacement)

def test_failed_persistence_changes_neither_memory_nor_runtime(config, tmp_path):
    api, binding = rig(config, tmp_path)
    before = copy.deepcopy(config)
    original = binding.adapter.config
    api.config_manager._persist_config = Mock(return_value=False)
    assert asyncio.run(update(api, {'max_inflight': 8})) is False
    assert config == before and binding.adapter.config is original


def test_disabled_link_is_read_only(config):
    config['private_island']['enabled'] = False
    with pytest.raises(ValueError): candidate(config, {'max_inflight': 8})
    assert not view(config, None)['editable']


def test_real_http_endpoint_schedules_status_and_save_on_daemon_loop(config, tmp_path):
    import cherrypy
    import threading
    from repeater.web.api_endpoints import APIEndpoints
    api, binding = rig(config, tmp_path)
    endpoint = APIEndpoints.__new__(APIEndpoints)
    endpoint.config = config
    endpoint.daemon_instance = api.daemon_instance
    endpoint.config_manager = api.config_manager
    endpoint._set_cors_headers = Mock()
    endpoint._require_post = Mock()
    endpoint.event_loop = asyncio.new_event_loop()
    started = threading.Event()
    def run():
        endpoint.event_loop.call_soon(started.set)
        endpoint.event_loop.run_forever()
    thread = threading.Thread(target=run)
    thread.start(); started.wait(2)
    try:
        cherrypy.request.method = 'GET'
        result = endpoint.internet_link()
        assert result['success'] and result['data']['listening']
        assert result['data']['peers'][0]['last_valid_inbound_frame_age_seconds'] is None
        cherrypy.request.method = 'POST'
        cherrypy.request.json = {'max_inflight': 8}
        assert endpoint.update_internet_link()['success']
        assert binding.adapter.config.max_inflight == 8
    finally:
        endpoint.event_loop.call_soon_threadsafe(endpoint.event_loop.stop)
        thread.join(2); endpoint.event_loop.close()
