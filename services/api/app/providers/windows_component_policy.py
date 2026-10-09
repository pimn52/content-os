"""Fixed owned policy2 bytes; never selected by an expected receipt or caller."""
import hashlib
import json
from pathlib import Path

from .runtime_primitives import NativeExecutionUnsupported, _exact_path

POLICY_SLOT = 'Lib/site-packages/app/providers/windows_component_policy.json'
POLICY_LIMIT = 65536


def owned_component_policy_bytes():
    try:
        path = Path(__file__).with_suffix('.json')
        _exact_path(path, directory=False)
        with path.open('rb') as source:
            payload = source.read(POLICY_LIMIT + 1)
        if not 0 < len(payload) <= POLICY_LIMIT: raise ValueError()
        value = json.loads(payload)
        expected = dict(architecture='amd64', context_directory='source_parent_by_role', code_file='comctl32.dll',
            code_name='Microsoft.Windows.Common-Controls',
            manifest_contract_sha256='bd76c737191489222cc219b605648fee50ae2721a7d1af7ebd756b429dee7ec2',
            identity_language='raw_exact_lowercase_directory', prediction_language='current_user_ui',
            maximum_resource_assemblies=1, policy_version=2, resource_file='comctl32.dll.mui',
            resource_name='Microsoft.Windows.Common-Controls.Resources', root_flags='opaque_uint32',
            token='6595b64144ccf1df')
        canonical = json.dumps(expected, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        if payload != canonical or value != expected: raise ValueError()
        return payload
    except (OSError, ValueError, TypeError, RuntimeError):
        raise NativeExecutionUnsupported('execution_component_policy_invalid') from None


def owned_component_policy_digest():
    return hashlib.sha256(owned_component_policy_bytes()).hexdigest()
