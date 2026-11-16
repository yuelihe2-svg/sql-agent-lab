"""Pinned upstream inputs, kept locally rather than redistributed or imported.

Only fetch() uses the network. Loading reads literal prompt constants with AST;
it never imports upstream Python (which can initialize providers).
"""
import argparse
import ast
import hashlib
from pathlib import Path
import urllib.request

REVISION = '31a9df5e0d520be4769be57a4b9022e5e34a14f4'
REPOSITORY = 'https://github.com/wbbeyourself/MAC-SQL'
HASHES = {
    'core/agents.py': 'cff334bda129999639b04b99e23cbe362a41a2809bbda1ab1477b41bf3760ee8',
    'core/const.py': 'dcf385e3f5cb13c12c418e8fd92b7b13d9cf1b470348414a750113e4d4ebaeae',
    'core/utils.py': '71dfd61513c4c46105aaf7d9d938b1a8f49cc70ed31879fc84dd1f23692bb99d',
    'core/chat_manager.py': 'e64610989ade880c46618e4909f4b1e727f3184ddf79442ede5178c13ce888eb',
    'core/llm.py': 'b013a05308a4374c3cdddbde15665271a410b6068815b19868cb5fbc0d6b9e3c',
}
PROMPTS = ('selector_template', 'decompose_template_bird', 'refiner_template')


def verified_sources(directory):
    sources = {}
    for name, expected in HASHES.items():
        data = (Path(directory) / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f'Upstream checksum mismatch: {name}')
        sources[name] = data.decode('utf8')
    return sources


def load_prompts(directory):
    sources = verified_sources(directory)
    constants = {}
    for node in ast.parse(sources['core/const.py']).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (*PROMPTS, 'MAX_ROUND'):
                    constants[target.id] = ast.literal_eval(node.value)
    if constants.get('MAX_ROUND') != 3 or any(
        not isinstance(constants.get(name), str) for name in PROMPTS
    ):
        raise ValueError('Unexpected upstream prompt contract')
    return {name: constants[name] for name in PROMPTS}


def fetch(directory):
    # Explicit command only; all downloads must match the reviewed source lock.
    directory = Path(directory)
    for name, expected in HASHES.items():
        request = urllib.request.Request(
            f'https://raw.githubusercontent.com/wbbeyourself/MAC-SQL/{REVISION}/{name}',
            headers={'User-Agent': 'SQL-Agent-Lab-reproduction'},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f'Download checksum mismatch: {name}')
        destination = directory / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return load_prompts(directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Fetch pinned public source; never call a model')
    parser.add_argument('command', choices=['fetch', 'verify'])
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    (fetch if args.command == 'fetch' else load_prompts)(args.directory)
    print(f'Upstream verified: {REVISION}')
