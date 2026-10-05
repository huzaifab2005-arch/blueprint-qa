"""Measuring on a sheet: scale, snapping, saved measurements and calibration."""
import asyncio
import os
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.assistant import DocumentPage, Measurement, SheetCalibration
from backend.routers.assistant import _page_geometry
from backend.schemas.measure import (
    CalibrationCreate, MeasurementCreate, MeasurementRead, MeasurementUpdate, ScaleRead, SheetScaleRead, SnapRead,
)
from backend.services.counting.pdf_geometry import PageGeometry
from backend.services.measure import ops
from backend.services.measure.scale import PT_PER_IN, ScaleInfo, SheetScales, analyse_sheet
from backend.services.measure.units import format_imperial, format_metric, parse_length

router = APIRouter(prefix="/api/measure", tags=["measure"])

# Scale analysis is a few hundred ms on a dense sheet, so it is kept per (pdf, page).
_ANALYSIS: dict[tuple, SheetScales] = {}


def _analyse(geom: PageGeometry, key: tuple) -> SheetScales:
    if key not in _ANALYSIS:
        if len(_ANALYSIS) > 64:
            _ANALYSIS.clear()
        _ANALYSIS[key] = analyse_sheet(geom)
    return _ANALYSIS[key]


def _calibration_scale(c: SheetCalibration, geom: PageGeometry) -> ScaleInfo:
    pts = ops.to_pt(geom, c.points)
    return ScaleInfo(
        ratio=c.ratio, text=f"Calibrated against {c.known_text}", source="calibrated", status="calibrated",
        support=1, system=c.system, anchors=[((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)],
    )


async def _scales_for(db: AsyncSession, document_id: uuid.UUID, page_number: int):
    """(geometry, sheet analysis, combined scale list, calibration ids by index, primary)."""
    page, geom = await _page_geometry(db, document_id, page_number)
    from backend.storage import get_storage
    from backend.models.document import Document
    document = await db.get(Document, document_id)
    path = await asyncio.to_thread(get_storage().get_path, document.file_path)
    key = (path, os.path.getmtime(path), page_number)
    sheet = await asyncio.to_thread(_analyse, geom, key)
    cals = (await db.execute(
        select(SheetCalibration)
        .where(SheetCalibration.document_id == document_id, SheetCalibration.page_number == page_number)
        .order_by(SheetCalibration.created_at)
    )).scalars().all()
    scales = list(sheet.scales)
    cal_ids: dict[int, uuid.UUID] = {}
    for c in cals:
        cal_ids[len(scales)] = c.id
        scales.append(_calibration_scale(c, geom))
    primary = sheet.primary
    if cals:                                  # a user's calibration is the most trustworthy scale there is
        primary = len(scales) - 1
    return geom, sheet, scales, cal_ids, primary


def _scale_read(geom: PageGeometry, sheet: SheetScales, scales: list[ScaleInfo], cal_ids, primary) -> SheetScaleRead:
    return SheetScaleRead(
        page_number=geom.page_number, width_pt=geom.width, height_pt=geom.height,
        scales=[
            ScaleRead(index=i, ratio=round(sc.ratio, 4), text=sc.text, source=sc.source, status=sc.status,
                      support=sc.support, system=sc.system, calibration_id=cal_ids.get(i))
            for i, sc in enumerate(scales)
        ],
        primary=primary, notes=sheet.notes, dimension_samples=sheet.samples,
    )


def _uncertainty_text(inches: float, system: str, is_area: bool) -> str:
    if inches <= 0:
        return ""
    if is_area:
        return ""
    txt = format_imperial(inches, 16) if system == "imperial" else format_metric(inches)
    return f"± {txt}"


def _to_read(m: Measurement, system: str = "imperial") -> MeasurementRead:
    is_area = m.kind == "area"
    perimeter = None
    if is_area and m.value_in is not None:
        perimeter = format_imperial(m.value_in) if system == "imperial" else format_metric(m.value_in)
    return MeasurementRead(
        id=m.id, page_number=m.page_number, kind=m.kind, label=m.label, points=m.points, value_in=m.value_in,
        value_sqin=m.value_sqin, display=m.display, display_other=m.display_other, perimeter_display=perimeter,
        uncertainty_in=m.uncertainty_in, uncertainty_display=_uncertainty_text(m.uncertainty_in, system, is_area),
        scale_ratio=m.scale_ratio, scale_text=m.scale_text, scale_status=m.scale_status,
        warnings=m.warnings or [], created_at=m.created_at,
    )


@router.get("/{document_id}/pages/{page_number}/scale", response_model=SheetScaleRead)
async def get_scale(document_id: uuid.UUID, page_number: int, db: AsyncSession = Depends(get_db)):
    """The sheet's scale(s), and how each was checked against the sheet's own dimensions."""
    geom, sheet, scales, cal_ids, primary = await _scales_for(db, document_id, page_number)
    return _scale_read(geom, sheet, scales, cal_ids, primary)


@router.get("/{document_id}/pages/{page_number}/snap", response_model=SnapRead)
async def get_snap_points(document_id: uuid.UUID, page_number: int, db: AsyncSession = Depends(get_db)):
    """Vertices of the drawing a click can snap to (fractions of the displayed page)."""
    _page, geom = await _page_geometry(db, document_id, page_number)
    pts = await asyncio.to_thread(ops.snap_points, geom)
    return SnapRead(page_number=page_number, count=len(pts),
                    points=[[round(x / geom.width, 5), round(y / geom.height, 5)] for x, y in pts])


def _validate_points(points: list[list[float]]) -> None:
    for p in points:
        if len(p) != 2 or not all(0.0 <= v <= 1.0 for v in p):
            raise HTTPException(status_code=422, detail="Points must be [x, y] fractions between 0 and 1.")


@router.post("/{document_id}/pages/{page_number}/measurements", response_model=MeasurementRead,
             status_code=status.HTTP_201_CREATED)
async def create_measurement(
    document_id: uuid.UUID, page_number: int, body: MeasurementCreate, db: AsyncSession = Depends(get_db)
):
    _validate_points(body.points)
    geom, sheet, scales, _cal_ids, primary = await _scales_for(db, document_id, page_number)
    if not scales:
        raise HTTPException(
            status_code=422,
            detail="No scale could be found for this sheet. Calibrate it first: mark a dimension you know "
                   "and enter its length.")
    pts = ops.to_pt(geom, body.points)
    idx = ops.choose_scale(scales, pts, primary, body.scale_index)
    scale = scales[idx]
    try:
        result = ops.measure(body.kind, pts, scale, body.snapped)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    warnings = list(result.warnings)
    if scale.status == "stated":
        warnings.append("This scale is only what the sheet says; nothing on the sheet confirms it. Check the result "
                        "against a known dimension, or calibrate.")
    elif scale.status == "conflict":
        warnings.append("This sheet's stated scale disagrees with its own dimensions; the result is likely wrong.")
    if len(sheet.scales) > 1 and body.scale_index is None and scale.source != "calibrated":
        warnings.append(f"This sheet has more than one scale; {scale.text} was used because it is nearest "
                        f"your measurement. Choose another if that is not the drawing you measured.")

    m = Measurement(
        document_id=document_id, page_number=page_number, kind=body.kind, label=body.label, points=body.points,
        value_in=result.value_in, value_sqin=result.value_sqin, display=result.display,
        display_other=result.display_other, uncertainty_in=result.uncertainty_in, scale_ratio=scale.ratio,
        scale_text=scale.text[:160], scale_status=scale.status, warnings=warnings,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    read = _to_read(m, scale.system)
    if result.perimeter_display:
        read.perimeter_display = result.perimeter_display
    return read


@router.get("/{document_id}/measurements", response_model=list[MeasurementRead])
async def list_measurements(
    document_id: uuid.UUID, page: int | None = None, db: AsyncSession = Depends(get_db)
):
    q = select(Measurement).where(Measurement.document_id == document_id)
    if page is not None:
        q = q.where(Measurement.page_number == page)
    rows = (await db.execute(q.order_by(Measurement.created_at))).scalars().all()
    return [_to_read(m) for m in rows]


@router.patch("/{document_id}/measurements/{measurement_id}", response_model=MeasurementRead)
async def rename_measurement(
    document_id: uuid.UUID, measurement_id: uuid.UUID, body: MeasurementUpdate, db: AsyncSession = Depends(get_db)
):
    m = await db.get(Measurement, measurement_id)
    if not m or m.document_id != document_id:
        raise HTTPException(status_code=404, detail="Measurement not found.")
    m.label = body.label
    await db.commit()
    await db.refresh(m)
    return _to_read(m)


@router.delete("/{document_id}/measurements/{measurement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_measurement(document_id: uuid.UUID, measurement_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    m = await db.get(Measurement, measurement_id)
    if not m or m.document_id != document_id:
        raise HTTPException(status_code=404, detail="Measurement not found.")
    await db.delete(m)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{document_id}/pages/{page_number}/calibrations", response_model=SheetScaleRead,
             status_code=status.HTTP_201_CREATED)
async def create_calibration(
    document_id: uuid.UUID, page_number: int, body: CalibrationCreate, db: AsyncSession = Depends(get_db)
):
    """Set the scale from a length you know: mark its two ends and say how long it is."""
    _validate_points(body.points)
    known = parse_length(body.length)
    if known is None or known <= 0:
        raise HTTPException(
            status_code=422,
            detail="Enter the length with its units, for example 12'-7\", 24\", 5'-0 1/2\", 1200 mm or 2.5 m.")
    page, geom = await _page_geometry(db, document_id, page_number)
    (x1, y1), (x2, y2) = ops.to_pt(geom, body.points)
    dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if dist < 5:
        raise HTTPException(status_code=422, detail="Those two points are too close together to calibrate from.")
    metric = bool(re.search(r"\b(?:mm|cm|m)\b", body.length.lower())) and "'" not in body.length and '"' not in body.length
    db.add(SheetCalibration(
        document_id=document_id, page_number=page_number, ratio=known / (dist / PT_PER_IN), known_length_in=known,
        known_text=body.length.strip(), points=body.points, system="metric" if metric else "imperial"))
    await db.commit()
    geom, sheet, scales, cal_ids, primary = await _scales_for(db, document_id, page_number)
    return _scale_read(geom, sheet, scales, cal_ids, primary)


@router.delete("/{document_id}/calibrations/{calibration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_calibration(document_id: uuid.UUID, calibration_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    c = await db.get(SheetCalibration, calibration_id)
    if not c or c.document_id != document_id:
        raise HTTPException(status_code=404, detail="Calibration not found.")
    await db.delete(c)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
