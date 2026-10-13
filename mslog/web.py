import json
import tempfile
from collections import Counter
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, send_from_directory, url_for)

from . import config, db, excel_in, export, ingest, masters, review, service, verify
from .review import FIELD_LABELS, ReviewError


def create_app(data_dir=None):
    data_dir = Path(data_dir or config.DATA_DIR)
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="mslog-local-mvp",
        DB_PATH=data_dir / "logistics.db",
        UPLOAD_DIR=data_dir / "uploads",
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
    )
    db.init_db(app.config["DB_PATH"])

    def conn():
        if "conn" not in g:
            g.conn = db.connect(app.config["DB_PATH"])
        return g.conn

    @app.teardown_appcontext
    def _close(_):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    def user():
        return request.cookies.get("operator") or "담당자"

    @app.template_filter("num")
    def num(v):
        return "" if v is None else format(v, ".10g")

    @app.template_filter("comma")
    def comma(v):
        return "" if v is None else f"{v:,.2f}".rstrip("0").rstrip(".")

    @app.context_processor
    def inject():
        return {"operator": user(), "FIELD_LABELS": FIELD_LABELS, "DOC_TYPES": config.DOC_TYPES,
                "ocr_enabled": config.OCR_PROVIDER == "claude"}

    def back(default="index"):
        return redirect(request.referrer or url_for(default))

    # ---- 납품자료 관리 ------------------------------------------------
    @app.get("/")
    def index():
        c = conn()
        q = (request.args.get("q") or "").strip()
        doc_type = request.args.get("doc_type") or ""
        state = request.args.get("state") or ""
        recs_by_doc = {}
        for r in c.execute("SELECT * FROM records WHERE review_status!='제외'"):
            recs_by_doc.setdefault(r["document_id"], []).append(r)
        open_by_rec = verify.open_status_by_record(c)
        docs = []
        for d in c.execute("SELECT * FROM documents ORDER BY id DESC"):
            rs = recs_by_doc.get(d["id"], [])
            if doc_type and d["doc_type"] != doc_type:
                continue
            hay = " ".join([d["file_name"]] + [str(r["vehicle_no"] or "") + str(r["customer_raw"] or "") for r in rs])
            if q and q not in hay:
                continue
            pending = sum(1 for r in rs if r["review_status"] == "미검수")
            errors = sum(1 for r in rs if open_by_rec.get(r["id"]) not in (None, "인식불확실"))
            if state == "pending" and not pending:
                continue
            if state == "error" and not errors:
                continue
            dates = sorted({r["delivery_date"] for r in rs if r["delivery_date"]})
            docs.append({
                "d": d, "n": len(rs), "pending": pending, "errors": errors,
                "dates": (dates[0] if len(dates) == 1 else f"{dates[0]} ~ {dates[-1]}") if dates else "",
                "vehicles": ", ".join(sorted({r["vehicle_no"] for r in rs if r["vehicle_no"]})),
            })
        counts = Counter(f["type"] for f in c.execute("SELECT type FROM findings WHERE status='open'"))
        has_c, has_i = masters.masters_loaded(c)
        return render_template("index.html", docs=docs, q=q, doc_type=doc_type, state=state,
                               counts=counts, order=verify.PRIORITY, has_c=has_c, has_i=has_i)

    @app.post("/operator")
    def set_operator():
        resp = redirect(request.referrer or url_for("index"))
        resp.set_cookie("operator", (request.form.get("operator") or "담당자").strip()[:30], max_age=60 * 60 * 24 * 365)
        return resp

    @app.post("/upload")
    def upload():
        doc_type = request.form.get("doc_type")
        files = [f for f in request.files.getlist("files") if f.filename]
        if not files:
            flash("업로드할 파일을 선택하세요.", "error")
            return redirect(url_for("index"))
        last = None
        for f in files:
            doc_id, msg = service.register_file(conn(), app.config["UPLOAD_DIR"], f.filename, f.read(), doc_type, user())
            flash(msg, "ok" if doc_id else "error")
            last = doc_id or last
        if len(files) == 1 and last:
            return redirect(url_for("document", doc_id=last))
        return redirect(url_for("index"))

    @app.get("/files/<int:doc_id>")
    def file(doc_id):
        d = conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if d is None:
            abort(404)
        return send_from_directory(app.config["UPLOAD_DIR"], d["stored_name"], download_name=d["file_name"])

    # ---- 검수 ---------------------------------------------------------
    @app.get("/documents/<int:doc_id>")
    def document(doc_id):
        c = conn()
        d = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if d is None:
            abort(404)
        open_by_rec = verify.open_status_by_record(c)
        recs = []
        for r in c.execute("SELECT * FROM records WHERE document_id=? ORDER BY id", (doc_id,)):
            fs = c.execute(
                """SELECT f.* FROM finding_records fr JOIN findings f ON f.id=fr.finding_id
                   WHERE fr.record_id=? ORDER BY f.status='open' DESC, f.id DESC""", (r["id"],)).fetchall()
            edits = c.execute("SELECT * FROM edits WHERE record_id=? ORDER BY id DESC", (r["id"],)).fetchall()
            recs.append({"r": r, "unc": set(json.loads(r["uncertain_fields"] or "[]")), "findings": fs,
                         "edits": edits, "status": open_by_rec.get(r["id"]),
                         "ai": json.loads(r["ai_json"] or "{}")})
        customers = c.execute("SELECT code,name FROM customers ORDER BY name").fetchall()
        items = c.execute("SELECT name FROM items ORDER BY name").fetchall()
        return render_template("document.html", d=d, recs=recs, customers=customers, items=items)

    def _handle(fn, *args):
        try:
            fn(*args)
            return True
        except (ReviewError, ValueError) as e:
            flash(str(e), "error")
            return False

    @app.post("/records/<int:rid>/save")
    def record_save(rid):
        try:
            n = review.save_record(conn(), rid, request.form, user())
            flash(f"저장했습니다({n}개 항목 변경). 변경된 기록은 다시 승인해야 합니다." if n else "변경 사항이 없습니다.", "ok")
        except ReviewError as e:
            flash(str(e), "error")
        return back()

    @app.post("/records/<int:rid>/approve")
    def record_approve(rid):
        # 화면에서 수정한 값이 있으면 먼저 저장한 뒤 승인한다.
        try:
            if request.form:
                review.save_record(conn(), rid, request.form, user())
            review.approve_record(conn(), rid, user())
            flash("검수 완료 처리했습니다.", "ok")
        except ReviewError as e:
            flash(str(e), "error")
        return back()

    @app.post("/records/<int:rid>/exclude")
    def record_exclude(rid):
        if _handle(review.exclude_record, conn(), rid, user(), request.form.get("reason")):
            flash("제외 처리했습니다.", "ok")
        return back()

    @app.post("/records/<int:rid>/restore")
    def record_restore(rid):
        if _handle(review.restore_record, conn(), rid, user()):
            flash("복원했습니다(미검수).", "ok")
        return back()

    @app.post("/documents/<int:doc_id>/add")
    def record_add(doc_id):
        c = conn()
        d = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if d is None:
            abort(404)
        ingest.add_record(c, doc_id, d["doc_type"], {"confidence": 1.0})
        c.commit()
        verify.run_verification(c)
        flash("빈 행을 추가했습니다. 원본을 보고 값을 입력하세요.", "ok")
        return back()

    @app.post("/documents/<int:doc_id>/reocr")
    def reocr(doc_id):
        c = conn()
        if c.execute("SELECT COUNT(*) FROM records WHERE document_id=?", (doc_id,)).fetchone()[0]:
            flash("이미 기록이 있어 재인식하지 않습니다.", "error")
        else:
            status, message, n = service.process_document(c, app.config["UPLOAD_DIR"], doc_id)
            flash(f"재인식: {status}" + (f" ({n}건)" if status == "완료" else f" - {message}"),
                  "ok" if status == "완료" else "error")
        return back()

    # ---- 오류 검증 ----------------------------------------------------
    @app.get("/findings")
    def findings():
        c = conn()
        ftype = request.args.get("type") or ""
        status = request.args.get("status") or "open"
        assignee = (request.args.get("assignee") or "").strip()
        sql, args = "SELECT * FROM findings WHERE 1=1", []
        if ftype:
            sql += " AND type=?"
            args.append(ftype)
        if status != "all":
            sql += " AND status=?"
            args.append(status)
        if assignee:
            sql += " AND assignee=?"
            args.append(assignee)
        rows = c.execute(sql + " ORDER BY delivery_date, vehicle_no, id", args).fetchall()
        rows.sort(key=lambda f: verify.PRIORITY.index(f["type"]))
        recmap = {}
        for fr in c.execute("SELECT fr.finding_id, r.id, r.document_id, r.doc_type FROM finding_records fr "
                            "JOIN records r ON r.id=fr.record_id"):
            recmap.setdefault(fr["finding_id"], []).append(fr)
        return render_template("findings.html", rows=rows, recmap=recmap, ftype=ftype, status=status,
                               assignee=assignee, order=verify.PRIORITY)

    @app.post("/findings/<int:fid>/assign")
    def finding_assign(fid):
        verify.assign_finding(conn(), fid, request.form.get("assignee"))
        return back("findings")

    @app.post("/findings/<int:fid>/resolve")
    def finding_resolve(fid):
        if _handle(verify.resolve_finding, conn(), fid, user(), request.form.get("resolution")):
            flash("조치 결과를 기록했습니다.", "ok")
        return back("findings")

    # ---- 집계·출력 ----------------------------------------------------
    @app.get("/reports")
    def reports():
        c = conn()
        d_from, d_to = request.args.get("from") or None, request.args.get("to") or None
        rows = export.verified_rows(c, d_from, d_to)
        pend = export.unverified_rows(c, d_from, d_to)
        return render_template("reports.html", rows=rows[:300], total=len(rows), pend=len(pend),
                               d_from=d_from or "", d_to=d_to or "")

    @app.get("/export.xlsx")
    def export_xlsx():
        d_from, d_to = request.args.get("from") or None, request.args.get("to") or None
        buf = export.build_workbook(conn(), d_from, d_to)
        name = f"납품물량_{d_from or '전체'}_{d_to or '전체'}.xlsx"
        return send_file(buf, as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ---- 기준정보 -----------------------------------------------------
    @app.get("/masters")
    def masters_page():
        c = conn()
        return render_template("masters.html",
                               customers=c.execute("SELECT * FROM customers ORDER BY code").fetchall(),
                               items=c.execute("SELECT * FROM items ORDER BY name").fetchall())

    @app.post("/masters/import")
    def masters_import():
        kind, f = request.form.get("kind"), request.files.get("file")
        if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
            flash("xlsx 파일을 선택하세요.", "error")
            return redirect(url_for("masters_page"))
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(f.read())
        try:
            rows = excel_in.read_table(tmp.name)
            n = masters.import_customers(conn(), rows) if kind == "customers" else masters.import_items(conn(), rows)
            flash(f"{n}건을 반영했습니다. 기존 기록에는 소급 적용되지 않으므로 '기록 재매칭'을 실행하세요.", "ok")
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        return redirect(url_for("masters_page"))

    @app.post("/masters/rematch")
    def masters_rematch():
        """기준정보 변경 후 미검수 기록의 거래처코드·품목을 다시 매칭한다."""
        c = conn()
        n = 0
        for r in c.execute("SELECT * FROM records WHERE review_status='미검수'").fetchall():
            code, name, _ = masters.match_customer(c, r["customer_raw"])
            item = masters.match_item(c, r["item"]) or r["item"]
            if (code, name, item) != (r["customer_code"], r["customer_name"], r["item"]):
                c.execute("UPDATE records SET customer_code=?, customer_name=?, item=? WHERE id=?",
                          (code, name, item, r["id"]))
                n += 1
        c.commit()
        verify.run_verification(c)
        flash(f"미검수 기록 {n}건을 다시 매칭했습니다.", "ok")
        return redirect(url_for("masters_page"))

    return app
