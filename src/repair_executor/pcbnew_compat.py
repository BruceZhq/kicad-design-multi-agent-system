"""Keep detached SWIG proxies alive for the lifetime of an isolated CAD job.

KiCad 9.0.2 can corrupt subsequent collection access when Remove() is called
over a temporary GetTracks() list and detached proxies are reclaimed mid-edit.
Retaining references does not change board contents or waive any validation.
The sandbox's memory/time limits bound their lifetime and resource use.
"""


def install():
    import pcbnew

    if getattr(pcbnew.BOARD.Remove, "_ratsnest_retains_detached", False):
        return
    original = pcbnew.BOARD.Remove
    detached = []

    def remove(board, item):
        result = original(board, item)
        detached.append((board, item))
        return result

    remove._ratsnest_retains_detached = True
    pcbnew.BOARD.Remove = remove
    # KiCad 9 accepts this obsolete overload but only asserts and leaves the
    # via unchanged. Fail early with actionable feedback instead of producing
    # a nominally successful script and invalid copper.
    via_width = pcbnew.PCB_VIA.SetWidth

    def set_via_width(via, *args):
        if len(args) == 1:
            raise ValueError("PCB_VIA.SetWidth requires (layer, diameter); use via.SetWidth(pcbnew.F_Cu, diameter)")
        return via_width(via, *args)

    pcbnew.PCB_VIA.SetWidth = set_via_width
