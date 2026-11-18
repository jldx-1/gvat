"""Portable, revision-checked review findings beside a sample's media and XML."""

import json
import math

from .workspace import WorkspaceError, atomic_write, digest, read_optional


class ReviewFile:
    def __init__(self, workspace):
        self.workspace = workspace
        self.path = workspace.source_root / 'reviews.json'
        self.revision = 'missing'

    def load(self, keys):
        remote = getattr(self.workspace, 'remote', None)
        if remote:
            remote.refresh_file(self.path)
        raw = read_optional(self.path)
        self.revision = digest(raw)
        if raw is None:
            return [], 0, 0
        try:
            data = json.loads(raw)
            if data.get('version') != 1 or not isinstance(data.get('issues'), list):
                raise ValueError('Unsupported review file version')
            issues, identifiers, comment_ids = [], set(), set()
            frames = {key: frame for frame, key in enumerate(keys)}
            for record in data['issues']:
                record = dict(record)
                image = record.pop('image')
                if image not in frames:
                    raise ValueError('A reviewed frame is missing from this sample')
                record['frame'] = frames[image]
                record['job'] = 1  # Every local workspace exposes the same native job route.
                identifier = record['id']
                if type(identifier) is not int or identifier <= 0 or identifier in identifiers:
                    raise ValueError('Invalid review identity')
                identifiers.add(identifier)
                points = record['position']
                if not isinstance(points, list) or len(points) < 4 or len(points) % 2 or any(type(p) not in (int, float) or not math.isfinite(p) for p in points):
                    raise ValueError('Invalid review region')
                if type(record['resolved']) is not bool or not isinstance(record['comments'], list):
                    raise ValueError('Invalid review finding')
                for comment in record['comments']:
                    identifier = comment['id']
                    if type(identifier) is not int or identifier <= 0 or identifier in comment_ids or not isinstance(comment['message'], str) or not comment['message'].strip():
                        raise ValueError('Invalid review comment')
                    comment_ids.add(identifier)
                issues.append(record)
            return issues, max(data.get('issue_counter', 0), max(identifiers, default=0)), max(data.get('comment_counter', 0), max(comment_ids, default=0))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise WorkspaceError(f'Invalid reviews.json: {error}. Restore or correct the review file before continuing.') from error

    def save(self, keys, issues, issue_counter, comment_counter):
        # Media-relative identities survive moving a sample to another checkout.
        records = [{**{key: value for key, value in issue.items() if key != 'frame'},
                    'image': keys[issue['frame']]} for issue in issues]
        raw = (json.dumps({'version': 1, 'issue_counter': issue_counter, 'comment_counter': comment_counter,
                           'issues': records}, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()
        remote = getattr(self.workspace, 'remote', None)
        if remote:
            remote.write_file(self.path, raw, self.revision)
        else:
            atomic_write(self.path, raw, self.revision)
        self.revision = digest(raw)
