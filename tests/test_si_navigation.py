def test_sciencedirect_issue_navigation_is_not_supplementary_material():
    from scanpdf._scansci.supplementary import extract_supplementary_links
    issue_links=''.join(f'<a href="/journal/chemical-physics-letters/vol/{n}/suppl/C">Supplement C</a>' for n in range(12))
    file='https://ars.els-cdn.com/content/image/1-s2.0-S0009261423001355-mmc1.docx'
    html=issue_links+f'<a href="{file}">Supplementary material</a>'
    assert extract_supplementary_links(html,'https://www.sciencedirect.com/science/article/pii/S0009261423001355')==[file]
    assert extract_supplementary_links('<a href="/doi/suppl/10.1002/test">SI</a>', 'https://onlinelibrary.wiley.com/doi/10.1002/test')==['https://onlinelibrary.wiley.com/doi/suppl/10.1002/test']
