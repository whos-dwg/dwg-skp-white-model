"""The writer's existing numeric grid must not turn empty references into errors."""
import ctypes as C
import os

import pytest

from dwg_semantic.skp_native import Native, Ref


def test_collapsed_reference_does_not_create_native_input():
    api=object.__new__(Native)
    api.create=lambda *args: pytest.fail('No geometry input is needed for a collapsed reference')
    api.geometry(Ref(),edges=[([123456.00000001,654321,0],[123456.00000002,654321,0])])


@pytest.mark.skipif(os.environ.get('DWG_RUN_NATIVE_TESTS')!='1',reason='Native runtime opt-in')
def test_actual_native_empty_reference_then_regular_edge():
    with Native() as api:
        model=api.create('SUModelCreate')
        try:
            entities=api.get('SUModelGetEntities',model)
            _,reference=api.group(entities,'Source reference retained',data={'id':'synthetic:empty-reference'})
            api.geometry(reference,edges=[([123456.00000001,654321,0],[123456.00000002,654321,0])])
            assert not api.edges(reference)
            api.geometry(reference,edges=[([123456,654321,0],[123556,654321,0])])
            assert len(api.edges(reference))==1
        finally:
            api.call('SUModelRelease',C.byref(model))


def test_invalid_face_is_not_silently_filtered():
    api=object.__new__(Native)
    calls=[]
    def reject_native_face(name,*args):
        calls.append(name)
        raise RuntimeError('native face processing still reached')
    api.create=reject_native_face
    with pytest.raises(RuntimeError,match='native face processing still reached'):
        api.geometry(Ref(),faces=[([[0,0,0],[0,0,0],[0,0,0]],[])])
    assert calls==['SUGeometryInputCreate']
