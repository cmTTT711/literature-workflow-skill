import json, hashlib, io, zipfile, importlib.util, ast
from pathlib import Path
import pytest
import pymupdf
from scanpdf.config import Config
from scanpdf.identity import Paper,stem,normalize_doi
from scanpdf.verification import verify_pdf,supplementary_type
from scanpdf.download import run,valid_resume
from scanpdf.publishers import route_for

D1="10.1234/alpha";D2="10.1234/beta"
TITLE="Magnetoelectric coupling in layered research materials"

def pdf(doi=D1,title=TITLE,pages=1):
 d=pymupdf.open()
 for i in range(pages):
  p=d.new_page();p.insert_text((72,72),title+"\n"+(doi if i==0 else "Reference discussion"))
 b=d.tobytes();d.close();return b

def test_pdf_identity_and_cross_doi_rejection():
 b=pdf();assert verify_pdf(b,D1,TITLE).status=="verified"
 assert verify_pdf(b,D2,TITLE).status!="verified"
 assert verify_pdf(b"<html>challenge</html>",D1,TITLE).status=="invalid"

def test_doi_only_in_late_references_does_not_verify():
 d=pymupdf.open();d.new_page().insert_text((72,72),"A different article")
 d.new_page().insert_text((72,72),"References: "+D1)
 assert verify_pdf(d.tobytes(),D1).status=="needs_review"

def test_supporting_pdf_rejected_as_main():
 assert verify_pdf(pdf(D1,"Supporting Information"),D1).reason=="supplement_in_main_slot"

def test_si_rejects_html_and_images_accepts_real_containers():
 assert supplementary_type(b"<html>blocked</html>","application/octet-stream","a.pdf") is None
 assert supplementary_type(b"GIF89a123", "image/gif") is None
 assert supplementary_type(pdf(),"application/octet-stream")==".pdf"
 buf=io.BytesIO()
 with zipfile.ZipFile(buf,"w") as z:z.writestr("xl/workbook.xml","<workbook/>")
 assert supplementary_type(buf.getvalue(),"application/octet-stream")==".xlsx"
 assert supplementary_type(b"PK\x03\x04broken","application/zip") is None

def test_school_backend_and_publisher_isolate_profiles(tmp_path):
 a=Config(school="学校甲",email="user@school.edu",home=tmp_path)
 b=Config(school="学校乙",email="user@school.edu",home=tmp_path)
 assert a.profile("rsc","camoufox")!=b.profile("rsc","camoufox")
 assert a.profile("rsc","camoufox")!=a.profile("rsc","patchright")
 a.save();assert Config.load(tmp_path).school==a.school
 assert "home" not in json.loads((tmp_path/"config.json").read_text(encoding="utf-8"))

def test_setup_rejects_placeholder_email(tmp_path):
 with pytest.raises(ValueError):Config(school="学校",email="a@example.invalid",home=tmp_path).save()

def test_doi_and_filename_normalization():
 assert normalize_doi("https://doi.org/10.1234/ABC")=="10.1234/abc"
 assert stem(D1)!=stem(D2)
 with pytest.raises(ValueError):normalize_doi("not a DOI")
 assert "/" not in stem("10.1234/../abc")

def test_backend_selection_and_aps_canonicalization():
 assert route_for("10.1039/d3cp00001a").backend=="camoufox"
 assert route_for("10.1016/j.test.2024.01.001").backend=="cloakbrowser"
 a=route_for("10.1103/physrevb.109.l100402")
 assert "/prb/" in a.article and "PhysRevB.109.L100402" in a.article

class FakeSession:
 main_calls=0;si_calls=0;article_dois=[]
 def __init__(self,route,cfg,emit):pass
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def fetch_main(self,paper,accept):
  type(self).main_calls+=1;self.article_dois.append(paper.doi)
  assert not accept(pdf("10.1234/wrong"),"https://publisher/wrong.pdf")
  assert accept(pdf(paper.doi,paper.title),"https://publisher/"+paper.doi+".pdf")
  return {"status":"ok"}
 def fetch_si(self,paper,out):
  type(self).si_calls+=1
  return {"status":"zero","files":[],"reason":"no_links_discovered"}

def test_pipeline_quarantines_wrong_pdf_and_rechecks_resume(tmp_path,monkeypatch):
 monkeypatch.setattr("scanpdf.download.metadata",lambda paper,config:paper)
 FakeSession.main_calls=0;FakeSession.si_calls=0;FakeSession.article_dois=[]
 cfg=Config(school="测试学校",email="user@school.edu",home=tmp_path/"home")
 out=tmp_path/"out";papers=[Paper(D1,TITLE),Paper(D2,TITLE)]
 result=run(papers,cfg,out,session_factory=FakeSession,emit=lambda _:None)
 assert result['main_ok']==2 and FakeSession.main_calls==2
 m=json.loads((out/"manifest.json").read_text(encoding="utf-8"));p=Path(m[D1]['path'])
 assert verify_pdf(p.read_bytes(),D1,TITLE).status=="verified"
 assert list((out/"quarantine").rglob("*.pdf"))
 # Main PDF is resumed, but missing/unresolved SI is processed again.
 run(papers,cfg,out,session_factory=FakeSession,emit=lambda _:None)
 assert FakeSession.main_calls==2 and FakeSession.si_calls==4
 p.write_bytes(pdf(D2))
 assert not valid_resume(m[D1],papers[0])
 run(papers,cfg,out,si=False,session_factory=FakeSession,emit=lambda _:None)
 assert FakeSession.main_calls==3
 assert not (out/".scanpdf-run.lock").exists()

def test_runtime_has_no_external_installed_plugin_imports():
 root=Path(__file__).resolve().parents[1]/"src"
 for path in root.rglob("*.py"):
  for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
   if isinstance(n,ast.ImportFrom) and n.level==0:assert not (n.module or "").startswith(("instsci","scansci_pdf")),str(path)
   if isinstance(n,ast.Import):assert not any(a.name.startswith(("instsci","scansci_pdf")) for a in n.names),str(path)


def test_pdf_discovery_works_without_optional_lxml():
 from scanpdf._scansci.publisher_pdf_router import discover_pdf_candidates_from_html
 html='<html><head><meta name="citation_pdf_url" content="https://publisher.test/paper.pdf"></head></html>'
 assert "https://publisher.test/paper.pdf" in discover_pdf_candidates_from_html(html,"https://publisher.test/article")


def test_si_discovery_excludes_wiley_figure_assets():
 from scanpdf.attachments import discover
 html='<a href="/cms/asset/id/paper-fig-0001-m.jpg">Figure</a><a href="/cms/asset/id/paper-sup-0001.pdf">Supporting information</a>'
 assert discover(html,"https://publisher.test/article")==["https://publisher.test/cms/asset/id/paper-sup-0001.pdf"]


def test_doi_boundary_preserves_following_line():
 d=pymupdf.open();p=d.new_page();p.insert_text((72,72),D1+"\nJournal of Research\n"+TITLE)
 assert verify_pdf(d.tobytes(),D1,TITLE).status=="verified"


def test_main_article_supporting_information_link_is_not_si_title():
 b=pdf(D1,TITLE+"\nSupporting Information\nJournal citation")
 assert verify_pdf(b,D1,TITLE).status=="verified"

def test_si_discovery_ignores_article_section_links():
 from scanpdf.attachments import discover
 html='<a href="https://publisher.test/article#MOESM1">Supplementary information</a><a href="/article">Supporting Information</a>'
 assert discover(html,"https://publisher.test/article")==[]


def test_si_fingerprint_deduplicates_metadata_but_retains_different_figures():
 from scanpdf.attachments import content_fingerprint
 a=pymupdf.open();p=a.new_page();p.insert_text((72,72),"Supporting information");p.draw_rect((100,100,200,200),color=(1,0,0))
 one=a.tobytes();a.set_metadata({"title":"Changed metadata"});two=a.tobytes()
 assert content_fingerprint(one,".pdf")==content_fingerprint(two,".pdf")
 p.draw_rect((300,300,400,400),color=(0,0,1));three=a.tobytes()
 assert content_fingerprint(one,".pdf")!=content_fingerprint(three,".pdf")


def test_aip_and_oxford_use_stable_profile_names():
 assert route_for("10.1063/5.0151740").publisher=="aip"
 assert route_for("10.1093/test.123").publisher=="oxford"


def test_scientific_title_subscripts_do_not_cause_false_mismatch():
 from scanpdf.verification import title_score
 title="Doping-induced magnetism and magnetoelectric coupling in one-dimensional NbOCl3and NbOBr3"
 extracted="Doping-induced magnetism and magnetoelectric coupling in one-dimensional NbOCl 3 and NbOBr 3"
 assert title_score(title,extracted)==1.0
 assert verify_pdf(pdf("",extracted),D1,title).status=="verified"
 assert verify_pdf(pdf(D2,extracted),D1,title).reason=="conflicting_first_page_doi"


def test_completed_si_redo_retires_only_unmodified_owned_files(tmp_path):
 from scanpdf.download import retire_replaced_si
 folder=tmp_path/'si';folder.mkdir()
 owned=folder/'owned.pdf';owned.write_bytes(b'old')
 edited=folder/'edited.pdf';edited.write_bytes(b'user changed')
 outside=tmp_path/'outside.pdf';outside.write_bytes(b'old')
 previous={'files':[{'path':str(p),'sha256':hashlib.sha256(b'old').hexdigest()} for p in [owned,edited,outside]]}
 retire_replaced_si(previous,{'status':'partial','files':[]},folder)
 assert owned.exists()
 retire_replaced_si(previous,{'status':'zero','files':[]},folder)
 assert not owned.exists() and edited.exists() and outside.exists()
