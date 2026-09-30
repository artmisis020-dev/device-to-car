"""Склеює MPEG-TS вихід одного кадру в один буфер для srtsink.

Навіщо: `mpegtsmux alignment=7` (попередня версія) відправляє лише повні
групи по 7 TS-пакетів (1316 байт = один SRT-пакет), а залишок кадру
тримає у внутрішньому адаптері до приходу НАСТУПНОГО кадру
(gstbasetsmux.c: gst_base_ts_mux_push_packets(force=FALSE)). Виміряно
30.09 (pcap на РПі + штрих-код у кадрі): останній байт кожного кадру йшов
у мережу на ~33мс (один кадр при 30fps) пізніше, ніж міг.

`alignment=0` віддає весь кадр одразу, але як список окремих 188-байтних
буферів, а srtsink не має render_list — кожен TS-пакет став би окремим
SRT-пакетом (у 7 разів більше пакетів і заголовків). Тому: mpegtsmux
alignment=0 → appsink (buffer-list) → тут склеюємо список в один буфер →
appsrc → srtsink, який сам ріже буфер на шматки по SRTO_PAYLOADSIZE (1316
= 7 TS-пакетів; gstsrtobject.c, gst_srt_object_write_one) — SRT-пакети
лишаються повнорозмірними, а хвіст кадру йде одразу, неповним пакетом."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Для рядка пайплайна: середина між mpegtsmux і srtsink.
APPSINK = (
    "appsink name=ts_sink emit-signals=true sync=false async=false "
    "buffer-list=true max-buffers=0 drop=false enable-last-sample=false"
)
# block=true + max-buffers=2: якщо SRT не встигає (вузький лінк), push
# блокує потік кодера — тиск доходить до `queue leaky=downstream` одразу за
# камерою, і там відкидаються старі кадри, як і в конвеєрі без appsrc. Інакше
# (block=false з великим max-bytes) кадри копичились би тут і затримка росла
# б на секунди.
APPSRC = (
    "appsrc name=ts_src is-live=true format=time do-timestamp=false "
    "block=true max-buffers=2 max-bytes=0 max-time=0 "
    "caps=\"video/mpegts,systemstream=(boolean)true,packetsize=(int)188\""
)


class _Packer:
    def __init__(self, Gst, src):
        self.Gst = Gst
        self.src = src

    def on_new_sample(self, sink):
        Gst = self.Gst
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffers = []
        blist = sample.get_buffer_list()
        if blist is not None:
            buffers = [blist.get(i) for i in range(blist.length())]
        else:
            buf = sample.get_buffer()
            if buf is not None:
                buffers = [buf]
        if not buffers:
            return Gst.FlowReturn.OK

        parts = []
        for buf in buffers:
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                parts.append(bytes(info.data))
            finally:
                buf.unmap(info)
        out = Gst.Buffer.new_wrapped(b"".join(parts))
        out.pts = buffers[0].pts
        out.dts = buffers[0].dts
        return self.src.emit("push-buffer", out)

    def on_eos(self, sink):
        del sink
        self.src.emit("end-of-stream")


def attach(pipeline) -> bool:
    sink = pipeline.get_by_name("ts_sink")
    src = pipeline.get_by_name("ts_src")
    if sink is None or src is None:
        return False
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    packer = _Packer(Gst, src)
    sink.connect("new-sample", packer.on_new_sample)
    sink.connect("eos", packer.on_eos)
    return True
