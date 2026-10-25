import struct,zlib
import numpy as np
from dwg_semantic.modeling import _depth_raster,_rgba_png


def test_per_pixel_depth_resolves_crossing_triangles_independent_of_input_order():
    points=[[1,1],[14,1],[1,14]]
    slope={'points':points,'depths':[0,13,0],'fill':'#ff0000'}
    level={'points':points,'depths':[5,5,5],'fill':'#0000ff'}
    a=_depth_raster([slope,level],16,16)
    b=_depth_raster([level,slope],16,16)
    assert np.array_equal(a,b)
    assert list(a[2,2])==[0,0,255,255]
    assert list(a[2,10])==[255,0,0,255]
    assert not a[15,15,3]


def test_adjacent_coplanar_triangles_cover_their_shared_edge_without_cracks():
    rows=[{'points':[[1,1],[14,1],[14,14]],'depths':[1,1,1],'fill':'#abcdef'},
          {'points':[[1,1],[14,14],[1,14]],'depths':[1,1,1],'fill':'#abcdef'}]
    rgba=_depth_raster(rows,16,16)
    assert (rgba[1:14,1:14,3]==255).all()


def test_transparent_depth_image_png_is_valid_lossless_rgba():
    rgba=np.array([[[10,20,30,255],[0,0,0,0]]],dtype=np.uint8)
    data=_rgba_png(rgba)
    assert data.startswith(b'\x89PNG\r\n\x1a\n')
    size=struct.unpack('!I',data[8:12])[0]
    assert struct.unpack('!IIBBBBB',data[16:16+size])==(2,1,8,6,0,0,0)
    offset=8+12+size
    length=struct.unpack('!I',data[offset:offset+4])[0]
    assert data[offset+4:offset+8]==b'IDAT'
    assert zlib.decompress(data[offset+8:offset+8+length])==b'\x00'+rgba.tobytes()
