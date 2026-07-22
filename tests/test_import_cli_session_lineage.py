import json


def test_import_cli_session_preserves_parent_session_id_workspace_and_project(tmp_path):
    from api.models import import_cli_session, SESSION_DIR, Session

    parent_id = 'parent_lineage_001'
    child_id = 'child_lineage_001'
    workspace = tmp_path / 'trusted-workspace'
    workspace.mkdir()

    # Ensure clean fixture state for direct model-level import.
    for sid in (parent_id, child_id):
        try:
            (SESSION_DIR / f'{sid}.json').unlink(missing_ok=True)
        except Exception:
            pass

    session = import_cli_session(
        child_id,
        'Child Session',
        [{'role': 'user', 'content': 'hello', 'timestamp': 1.0}],
        model='test-model',
        parent_session_id=parent_id,
        created_at=1.0,
        updated_at=2.0,
        workspace=workspace,
        project_id='trusted-project',
    )

    assert session.parent_session_id == parent_id
    assert session.workspace == str(workspace.resolve())
    assert session.project_id == 'trusted-project'

    payload = json.loads((SESSION_DIR / f'{child_id}.json').read_text(encoding='utf-8'))
    assert payload['parent_session_id'] == parent_id
    assert payload['workspace'] == str(workspace.resolve())
    assert payload['project_id'] == 'trusted-project'

    loaded = Session.load(child_id)
    assert loaded.parent_session_id == parent_id
    assert loaded.workspace == str(workspace.resolve())
    assert loaded.project_id == 'trusted-project'
    assert loaded.compact()['parent_session_id'] == parent_id
