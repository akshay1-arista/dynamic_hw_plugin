from collections import Counter
import json

import pytest

from app.discovery import DiscoveryDeviceNotFound, DiscoveryError, apply_inventory_refresh, preview_inventory_refresh
from app.discovery_runtime import operation_status
from app.inventory import load_inventory, save_inventory, update_hardware_availability
from app.models import ActorIdentity, InventoryRefreshRequest


def entry(local, peer, remote=None):
    return {"interface_name": local, "remote_device": peer, "remote_interface_name": remote}


class ReconciliationClient:
    def __init__(self, ports=1):
        self.calls = Counter()
        self.new = {"id": 2, "name": "new-edge", "device_type": "edge", "device_model": "edge7X0", "serial_number": "NEW"}
        self.old = {"id": 1, "name": "old-edge", "device_type": "edge", "device_model": "edge7X0", "serial_number": "OLD"}
        self.switch = {"id": 11, "name": "access-switch", "device_type": "switch", "device_model": "Dell-3048"}
        self.maps = {
            2: {"connections": [entry(f"GE{number}", self.switch, f"Gi1/{number}") for number in range(1, ports + 1)]},
            11: {"connections": [entry(f"Gi1/{number}", self.new, f"GE{number}") for number in range(1, ports + 1)]},
            1: {"connections": [entry(f"GE{number}", None) for number in range(1, ports + 1)]},
        }

    def get_wiremap(self, identifier):
        self.calls[identifier] += 1
        result = self.maps[identifier]
        if isinstance(result, Exception):
            raise result
        return result


def inventory_path(tmp_path, ports=1, manual=False, ha=False):
    path = tmp_path / 'inventory.json'
    devices = {
        'old': {'id': 'old', 'type': 'edge', 'display_name': 'old-edge', 'model': 'edge7X0', 'serial_number': 'OLD', 'lab_navigator_id': 1},
        'new': {'id': 'new', 'type': 'edge', 'display_name': 'new-edge', 'model': 'edge7X0', 'serial_number': 'NEW', 'lab_navigator_id': 2},
        'switch': {'id': 'switch', 'type': 'switch', 'display_name': 'access-switch', 'model': 'Dell-3048', 'lab_navigator_id': 11,
                   'switch_metadata': {'name': 'access-switch', 'model': 'Dell-3048', 'connections': {'ip': '192.0.2.1'},
                                       'credentials': {'username': 'test-user', 'password': 'test-password'}}},
    }
    if ha:
        devices['old'].update(ha_group_id='old-pair', ha_role='active')
        devices['standby'] = {'id': 'standby', 'type': 'edge', 'display_name': 'old-standby', 'model': 'edge7X0',
                              'serial_number': 'STANDBY', 'lab_navigator_id': 3, 'ha_group_id': 'old-pair', 'ha_role': 'standby'}
    connections = [
        {'id': f'old-{number}', 'a': {'device_id': 'old', 'interface': f'GE{number}'},
         'b': {'device_id': 'switch', 'interface': f'gigabitethernet1/{number}'},
         'role': 'edge-access', 'notes': 'Manual cable' if manual else 'Imported from Lab Navigator wiremap.'}
        for number in range(1, ports + 1)
    ]
    path.write_text(json.dumps({'devices': devices, 'connections': connections}))
    save_inventory(load_inventory(path), path)
    return path


@pytest.mark.parametrize('ha', [False, True])
def test_reconcile_eight_links_preview_apply_preserves_reservations(tmp_path, ha):
    path = inventory_path(tmp_path, ports=8, ha=ha)
    old_group = 'old-pair' if ha else 'old'
    update_hardware_availability(old_group, False, ActorIdentity(name='Other User', email='other@example.com'), path)
    before = path.read_bytes()
    client = ReconciliationClient(ports=8)
    result = preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=['new']), inventory_path=path, client=client)
    assert path.read_bytes() == before
    assert result.summary.status == 'success'
    assert result.summary.reconciled_connection_count == 8
    assert result.summary.skipped_conflicting_endpoint_count == 0
    assert len([c for c in result.changes if c.change_type == 'remove-connection']) == 8
    assert all(c.change_type == 'remove-connection' for c in result.changes[:8])
    assert len([c for c in result.inventory.connections if c.a.device_id == 'new']) == 8
    assert not any(c.id.startswith('old-') for c in result.inventory.connections)
    assert client.calls[1] == 1
    logs = operation_status(result.request_id)['logs']
    assert sum('Reconciled stale link on' in event['message'] for event in logs) == 8
    assert any('Reconciled 8 stale wiremap link(s)' in message.message for message in result.messages)
    applied = apply_inventory_refresh(InventoryRefreshRequest(hardware_ids=['new'], preview_id=result.preview_id), inventory_path=path, client=object())
    old = next(h for h in applied.inventory.hardware if h.id == old_group)
    assert not old.available
    assert old.reservation.actor.email == 'other@example.com'
    assert applied.inventory.devices['switch'].switch_metadata.credentials.password == 'test-password'
    assert len(next(h for h in applied.inventory.hardware if h.id == 'new').ports) == 8
    if ha:
        assert applied.inventory.devices['standby'].ha_group_id == 'old-pair'


@pytest.mark.parametrize('case', [
    'old_still_claims', 'old_failed', 'old_missing', 'old_port_omitted', 'old_malformed', 'old_peer_omitted',
    'old_peer_incomplete', 'old_duplicate', 'old_wrong_header', 'switch_disagrees', 'switch_wrong_port',
    'switch_port_omitted', 'switch_failed', 'manual',
])
def test_uncertain_conflicts_are_preserved(tmp_path, case):
    path = inventory_path(tmp_path, manual=case == 'manual')
    client = ReconciliationClient()
    if case == 'old_still_claims':
        client.maps[1]['connections'] = [entry('GE1', client.switch, 'Gi1/1')]
    elif case == 'old_failed':
        client.maps[1] = DiscoveryError('Request failed')
    elif case == 'old_missing':
        client.maps[1] = DiscoveryDeviceNotFound('Device not found')
    elif case == 'old_port_omitted':
        client.maps[1] = {'connections': []}
    elif case == 'old_malformed':
        client.maps[1] = {'connections': None}
    elif case == 'old_peer_omitted':
        client.maps[1] = {'connections': [{'interface_name': 'GE1'}]}
    elif case == 'old_peer_incomplete':
        client.maps[1] = {'connections': [entry('GE1', client.switch)]}
    elif case == 'old_duplicate':
        client.maps[1]['connections'].append(entry('GE1', client.switch, 'Gi1/1'))
    elif case == 'old_wrong_header':
        client.maps[1]['device'] = {'id': 999}
    elif case == 'switch_disagrees':
        client.maps[11]['connections'][0]['remote_device'] = client.old
    elif case == 'switch_wrong_port':
        client.maps[11]['connections'][0]['remote_interface_name'] = 'GE2'
    elif case == 'switch_port_omitted':
        client.maps[11] = {'connections': []}
    elif case == 'switch_failed':
        client.maps[11] = DiscoveryError('Switch unavailable')
    result = preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=['new']), inventory_path=path, client=client)
    assert result.summary.status == 'partial'
    assert result.summary.reconciled_connection_count == 0
    assert any(c.id == 'old-1' for c in result.inventory.connections)
    assert not any(c.a.device_id == 'new' for c in result.inventory.connections)
    assert not any(c.change_type == 'remove-connection' for c in result.changes)
    assert client.calls[1] <= 1


def test_old_edge_recabled_elsewhere_allows_reconciliation(tmp_path):
    path = inventory_path(tmp_path)
    client = ReconciliationClient()
    client.maps[1]['connections'][0] = entry('GE1', {**client.switch, 'id': 99, 'name': 'other-switch'}, 'Gi1/9')
    result = preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=['new']), inventory_path=path, client=client)
    assert result.summary.status == 'success'
    assert result.summary.reconciled_connection_count == 1


def test_unrelated_conflict_does_not_mark_selected_target_partial(tmp_path):
    path = inventory_path(tmp_path)
    data = json.loads(path.read_text())
    data['devices']['unrelated-old'] = {**data['devices']['old'], 'id': 'unrelated-old', 'lab_navigator_id': 4, 'display_name': 'unrelated-old'}
    data['devices']['unrelated-new'] = {**data['devices']['new'], 'id': 'unrelated-new', 'lab_navigator_id': 5, 'display_name': 'unrelated-new'}
    data['connections'].append({'id': 'unrelated-old-link', 'a': {'device_id': 'unrelated-old', 'interface': 'GE1'},
                                'b': {'device_id': 'switch', 'interface': 'gigabitethernet1/2'},
                                'role': 'edge-access', 'notes': 'Imported from Lab Navigator wiremap.'})
    path.write_text(json.dumps(data))
    client = ReconciliationClient()
    client.maps[11]['connections'].append(entry('Gi1/2', {**client.new, 'id': 5, 'name': 'unrelated-new'}, 'GE1'))
    result = preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=['new']), inventory_path=path, client=client)
    assert result.summary.status == 'success'
    assert result.summary.skipped_conflicting_endpoint_count == 0
    assert result.summary.reconciled_connection_count == 1
    assert any(c.id == 'unrelated-old-link' for c in result.inventory.connections)
    assert client.calls[4] == 0


def test_failed_old_wiremap_is_loaded_only_once_for_eight_conflicts(tmp_path):
    path = inventory_path(tmp_path, ports=8)
    client = ReconciliationClient(ports=8)
    client.maps[1] = DiscoveryError('Unavailable')
    result = preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=['new']), inventory_path=path, client=client)
    assert result.summary.status == 'partial'
    assert result.summary.skipped_conflicting_endpoint_count == 8
    assert client.calls[1] == 1


def test_switch_only_claim_without_forward_edge_confirmation_is_protected(tmp_path):
    path = inventory_path(tmp_path)
    client = ReconciliationClient()
    client.maps[2]['connections'] = [entry('GE2', client.switch, 'Gi1/2')]
    client.maps[11]['connections'].append(entry('Gi1/2', client.new, 'GE2'))
    result = preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=['new']), inventory_path=path, client=client)
    assert result.summary.status == 'partial'
    assert result.summary.reconciled_connection_count == 0
    assert any(c.id == 'old-1' for c in result.inventory.connections)
    assert [p.logical_interface for p in next(h for h in result.inventory.hardware if h.id == 'new').ports] == ['GE2']
