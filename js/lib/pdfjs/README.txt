pdf.js 6.3.289, Mozilla's PDF reader, used by tools/pdf-to-image.html only.

Source:    the npm package pdfjs-dist 6.3.289
           https://registry.npmjs.org/pdfjs-dist/-/pdfjs-dist-6.3.289.tgz
Integrity: sha512-ZHjSVpDa3D6izMq8/04lvkhkATUmL9px6ChPaXc1k6nU2Mrhlg1/7F0bdUqCwUjw3NsPTfPZsMDUU6ZIcRaeQw==
           (checked against the registry's own value when it was downloaded, 2 Oct 2026)
Licence:   Apache License 2.0 (LICENSE). The fonts, colour profile and
           picture decoders carry their own licences beside them.

Copied unchanged from the package:
  build/pdf.min.mjs, build/pdf.worker.min.mjs, LICENSE,
  cmaps/, standard_fonts/, wasm/, iccs/

The page loads it only when a PDF is chosen. To update: download the new
pdfjs-dist tarball, check its integrity against the registry, copy the same
files, and run test_tools.py pdf-to-image and the Windows comparison again.
