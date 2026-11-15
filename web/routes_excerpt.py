@app.get("/site/admin/maintenance")
@admin_required
def admin_maintenance():
    import maintenance as mt
    report, analysis = mt.load(mt.REPORT), mt.load(mt.ANALYSIS)
    if analysis and report and analysis.get("for") != report.get("collected_at"):
        analysis = None  # stale: belongs to an older report
    return render_template("admin/maintenance.html", tab="maintenance", kinds=KINDS, r=report, a=analysis,
                           pending=mt.pending(), analysing=mt.analysing(), ai=bool(mt.GEMINI_KEY), model=mt.GEMINI_MODEL,
                           accepted=mt.accepted())


@app.get("/site/admin/maintenance/status.json")
@admin_required
def admin_maintenance_status():
    import maintenance as mt
    return mt.status()


@app.post("/site/admin/maintenance/<action>")
@admin_required
def admin_maintenance_action(action):
    import maintenance as mt
    if action == "run":
        mt.request_run()
        open(mt.AUTO, "w").close()
    elif action == "analyse":
        report = mt.load(mt.REPORT)
        if not report:
            flash("Run a diagnostic first.")
        else:
            mt.analyse_async(report)
            pass
    elif action == "accepted":
        os.makedirs(mt.DIAG, exist_ok=True)
        with open(mt.ACCEPTED, "w") as f:
            f.write(request.form.get("accepted", "").strip()[:4000] + "\n")
        flash("Saved. The AI will not report these again.")
    return redirect(url_for("admin_maintenance"))

