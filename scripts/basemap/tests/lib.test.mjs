// node --test scripts/basemap/tests  (Node 24, no npm dependencies; also the infra/basemap/Dockerfile `test` stage)
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import {
  ALLOWED_HOSTS,
  archivePath,
  archivesToPrune,
  assertAllowedUrl,
  assetPlan,
  buildManifest,
  checkBudget,
  extractArgs,
  fetchAllowed,
  FONTS,
  gitBlobSha1,
  isSafeRelativePath,
  manifestProblems,
  mapLimit,
  parseArgs,
  parseDryRun,
  rawUrl,
  reduceLand,
  resolveBuild,
  resolveLink,
  UsageError,
  writeFileAtomic,
} from "../lib.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE =
  process.env.BASEMAP_MANIFEST_FIXTURE || path.join(here, "..", "..", "..", "packages", "viz", "tests", "fixtures", "basemap-manifest.json");

/** Real `pmtiles extract --dry-run` log (go-pmtiles v1.31.2, build 20261009, maxzoom 5). */
const DRY_RUN_LOG = `2026/10/09 18:24:47 extract.go:401: fetching 1 dirs, 1 chunks, 1 requests
2026/10/09 18:24:48 extract.go:441: Region tiles 1365, result tile entries 1047
2026/10/09 18:24:48 extract.go:450: fetching 1047 tiles, 1 chunks, 1 requests
2026/10/09 18:24:48 extract.go:606: Completed in 2.045730885s with 4 download threads (511.79749814465464 tiles/s).
2026/10/09 18:24:48 extract.go:611: Extract required 5 total requests.
2026/10/09 18:24:48 extract.go:612: Extract transferred 15 MB (overfetch 0.05) for an archive size of 15 MB
`;

const BUILDS = [
  { key: "20240115.pmtiles", size: 118079300200, version: "3.1.0" },
  { key: "20261007.pmtiles", size: 138700000000, version: "4.15.2", b3sum: "a".repeat(64) },
  { key: "20261009.pmtiles", size: 138736840194, version: "4.15.2", b3sum: "66eb2acd67e4812c80f817d400906666260f1d25600a098717918095e725ba7b" },
  { key: "20261008.pmtiles", size: 138711111111, version: "4.15.2" },
  { key: "20261010.pmtiles", size: 140000000000, version: "5.0.0" },
  { key: "garbage", size: 1, version: "4.0.0" },
];

describe("parseArgs", () => {
  it("takes env defaults and flags (both --x v and --x=v)", () => {
    assert.deepEqual(parseArgs([], {}), {
      profile: "standard",
      build: "latest",
      maxMb: 300,
      keep: 1,
      dryRun: false,
      check: false,
      help: false,
      out: "/srv/basemap",
    });
    const o = parseArgs(["--profile", "lite", "--build=20261009", "--max-mb", "50", "--dry-run"], { BASEMAP_PROFILE: "detail" });
    assert.equal(o.profile, "lite");
    assert.equal(o.build, "20261009");
    assert.equal(o.maxMb, 50);
    assert.equal(o.dryRun, true);
    assert.equal(parseArgs([], { BASEMAP_PROFILE: "detail", BASEMAP_MAX_MB: "400", BASEMAP_DIR: "/data" }).out, "/data");
  });

  for (const [argv, env] of [
    [["--profile", "huge"], {}],
    [["--build", "yesterday"], {}],
    [["--max-mb", "0"], {}],
    [["--max-mb", "12.5"], {}],
    [["--keep", "0"], {}],
    [["--dry-run", "--check"], {}],
    [["--frobnicate"], {}],
    [["--profile"], {}],
    [[], { BASEMAP_MAX_MB: "lots" }],
  ]) {
    it(`rejects ${JSON.stringify(argv)} ${JSON.stringify(env)}`, () => {
      assert.throws(() => parseArgs(argv, env), UsageError);
    });
  }
});

describe("build resolution", () => {
  it("latest = newest tiles v4 build (v3 and v5 ignored)", () => {
    const { build, warning } = resolveBuild(BUILDS, "latest");
    assert.equal(build.key, "20261009.pmtiles");
    assert.equal(warning, null);
  });

  it("a pinned build is used as is", () => {
    assert.equal(resolveBuild(BUILDS, "20261007").build.key, "20261007.pmtiles");
  });

  it("a pinned build that aged out falls back to latest with a warning", () => {
    const { build, warning } = resolveBuild(BUILDS, "20260101");
    assert.equal(build.key, "20261009.pmtiles");
    assert.match(warning, /20260101 is no longer published; using the latest build 20261009/);
  });

  it("a pinned build of another tiles major is refused", () => {
    assert.throws(() => resolveBuild(BUILDS, "20240115"), /tiles v3\.1\.0/);
    assert.throws(() => resolveBuild(BUILDS, "20261010"), /tiles v5\.0\.0/);
  });

  it("an index without a v4 build is an error", () => {
    assert.throws(() => resolveBuild([{ key: "20240115.pmtiles", size: 1, version: "3.1.0" }], "latest"), /no Protomaps build with tiles v4/);
    assert.throws(() => resolveBuild({}, "latest"), /not a list/);
  });

  it("names archives by build and profile", () => {
    assert.equal(archivePath(BUILDS[2], "standard"), "v4/keelson-20261009-standard.pmtiles");
  });
});

describe("extract plan", () => {
  it("gives the exact go-pmtiles argv per profile", () => {
    const src = "https://build.protomaps.com/20261009.pmtiles";
    assert.deepEqual(extractArgs({ sourceUrl: src, out: "/srv/basemap/.staging-1/archive.pmtiles", profile: "lite" }), [
      "extract",
      src,
      "/srv/basemap/.staging-1/archive.pmtiles",
      "--minzoom=0",
      "--maxzoom=5",
      "--download-threads=4",
    ]);
    assert.equal(extractArgs({ sourceUrl: src, out: "x", profile: "standard" })[4], "--maxzoom=6");
    assert.deepEqual(extractArgs({ sourceUrl: src, out: "x", profile: "detail", dryRun: true }).slice(4), ["--maxzoom=7", "--download-threads=4", "--dry-run"]);
    assert.throws(() => extractArgs({ sourceUrl: src, out: "x", profile: "huge" }), UsageError);
  });

  it("reads the size estimate of a dry run", () => {
    assert.equal(parseDryRun(DRY_RUN_LOG), 15_000_000);
    assert.equal(parseDryRun("… for an archive size of 1.2 GB"), 1_200_000_000);
    assert.equal(parseDryRun("… for an archive size of 890 kB"), 890_000);
    assert.equal(parseDryRun("… for an archive size of 2 MiB"), 2 * 1024 * 1024);
    assert.throws(() => parseDryRun("Completed in 2s"), /could not read the archive size/);
  });

  it("enforces the size budget and the free space", () => {
    assert.equal(checkBudget({ estimateBytes: 45e6, maxMb: 300 }), 59e6);
    assert.throws(() => checkBudget({ estimateBytes: 189e6, maxMb: 200 }), /needs about 203 MB .* BASEMAP_MAX_MB is 200 MB/);
    assert.throws(() => checkBudget({ estimateBytes: 15e6, maxMb: 20 }), /BASEMAP_MAX_MB/);
    assert.throws(() => checkBudget({ estimateBytes: 45e6, maxMb: 300, freeBytes: 80e6 }), /80 MB free/);
    assert.equal(checkBudget({ estimateBytes: 45e6, maxMb: 300, freeBytes: 200e6 }), 59e6);
  });
});

describe("integrity and egress", () => {
  it("computes git blob ids", () => {
    assert.equal(gitBlobSha1("hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a");
    assert.equal(gitBlobSha1(Buffer.alloc(0)), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391");
  });

  it("allows only the pinned https hosts", () => {
    assert.deepEqual(ALLOWED_HOSTS, ["build.protomaps.com", "build-metadata.protomaps.dev", "api.github.com", "raw.githubusercontent.com"]);
    assert.equal(assertAllowedUrl("https://build.protomaps.com/20261009.pmtiles").hostname, "build.protomaps.com");
    for (const bad of [
      "http://build.protomaps.com/x",
      "https://evil.example/x",
      "https://build.protomaps.com.evil.example/x",
      "https://user:pw@raw.githubusercontent.com/x",
      "https://raw.githubusercontent.com:8443/x",
      "https://169.254.169.254/latest/meta-data",
      "file:///etc/passwd",
      "not a url",
    ]) {
      assert.throws(() => assertAllowedUrl(bad), /refusing|invalid URL/, bad);
    }
  });

  const fakeFetch = (routes) => {
    const calls = [];
    const fn = async (url, init) => {
      calls.push([url, init]);
      const r = routes[url] ?? { status: 404 };
      return new Response(r.body ?? null, { status: r.status, headers: r.location ? { location: r.location } : {} });
    };
    fn.calls = calls;
    return fn;
  };

  it("follows redirects only to allowed hosts, by hand", async () => {
    const ok = fakeFetch({
      "https://api.github.com/a": { status: 302, location: "https://raw.githubusercontent.com/b" },
      "https://raw.githubusercontent.com/b": { status: 200, body: "fine" },
    });
    assert.equal(await (await fetchAllowed("https://api.github.com/a", { fetchImpl: ok })).text(), "fine");
    assert.equal(ok.calls[0][1].redirect, "manual");

    const evil = fakeFetch({ "https://api.github.com/a": { status: 301, location: "https://evil.example/x" } });
    await assert.rejects(fetchAllowed("https://api.github.com/a", { fetchImpl: evil }), /refusing to fetch https:\/\/evil\.example/);
    assert.equal(evil.calls.length, 1);

    const meta = fakeFetch({ "https://api.github.com/a": { status: 307, location: "http://169.254.169.254/" } });
    await assert.rejects(fetchAllowed("https://api.github.com/a", { fetchImpl: meta }), /refusing/);

    const loop = fakeFetch({ "https://api.github.com/a": { status: 302, location: "/a" } });
    await assert.rejects(fetchAllowed("https://api.github.com/a", { fetchImpl: loop }), /too many/);

    await assert.rejects(fetchAllowed("https://api.github.com/missing", { fetchImpl: fakeFetch({}) }), /answered 404/);
  });

  it("builds raw URLs with encoded path segments", () => {
    assert.equal(
      rawUrl("protomaps/basemaps-assets", "028c18f7", "fonts/Noto Sans Regular/0-255.pbf"),
      "https://raw.githubusercontent.com/protomaps/basemaps-assets/028c18f7/fonts/Noto%20Sans%20Regular/0-255.pbf",
    );
  });

  it("resolves glyph symlinks inside fonts/ only", () => {
    assert.equal(
      resolveLink("fonts/Noto Sans Devanagari Regular v1/0-255.pbf", "../Noto Sans Regular/0-255.pbf"),
      "fonts/Noto Sans Regular/0-255.pbf",
    );
    assert.equal(resolveLink("fonts/X/0-255.pbf", "../../../etc/passwd"), null);
    assert.equal(resolveLink("fonts/X/0-255.pbf", "/etc/passwd"), null);
    assert.equal(resolveLink("fonts/X/0-255.pbf", "..\\..\\etc"), null);
    assert.equal(resolveLink("fonts/X/0-255.pbf", "../../sprites/v4/light.png"), null);
  });

  it("knows safe relative paths", () => {
    for (const ok of ["v4/keelson-20261009-lite.pmtiles", "fonts/Noto Sans Regular/0-255.pbf", "sprites/v4/light@2x.png"]) assert.ok(isSafeRelativePath(ok), ok);
    for (const bad of ["/abs", "../x", "a/../b", "./a", "a//b", "a\\b", "https://x/y", "%2e%2e/x", "", "v4/{z}/x.pmtiles"]) assert.ok(!isSafeRelativePath(bad), bad);
  });
});

describe("asset plan", () => {
  const ranges = Array.from({ length: 256 }, (_, i) => `${i * 256}-${i * 256 + 255}.pbf`);
  const tree = () => ({
    truncated: false,
    tree: [
      ...FONTS.flatMap((font) =>
        ranges.map((r, i) => ({
          path: `fonts/${font}/${r}`,
          type: "blob",
          mode: font.includes("Devanagari") && i !== 9 ? "120000" : "100644",
          sha: `${i}`.padStart(40, "0"),
          size: 10,
        })),
      ),
      { path: "fonts/Noto Sans Regular", type: "tree", mode: "040000", sha: "t" },
      ...["light", "dark", "white"].flatMap((s) => ["", "@2x"].flatMap((d) => [".json", ".png"].map((e) => ({ path: `sprites/v4/${s}${d}${e}`, type: "blob", mode: "100644", sha: "s" })))),
      { path: "fonts/OFL.txt", type: "blob", mode: "100644", sha: "o" },
      { path: "README.md", type: "blob", mode: "100644", sha: "r" },
    ],
  });

  it("takes every glyph range of the four stacks, light/dark sprites at 1x/2x and the font licence", () => {
    const { files, links } = assetPlan(tree());
    assert.equal(links.length, 255);
    assert.equal(files.length, 3 * 256 + 1 + 8 + 1);
    assert.ok(files.some((f) => f.path === "fonts/OFL.txt"));
    assert.ok(files.some((f) => f.path === "sprites/v4/dark@2x.png"));
    assert.ok(!files.some((f) => f.path.includes("white") || f.path === "README.md"));
  });

  it("refuses incomplete or truncated listings", () => {
    assert.throws(() => assetPlan({ ...tree(), truncated: true }), /truncated/);
    const t = tree();
    t.tree = t.tree.filter((e) => e.path !== "sprites/v4/light@2x.png");
    assert.throws(() => assetPlan(t), /no file sprites\/v4\/light@2x\.png/);
    const t2 = tree();
    t2.tree = t2.tree.filter((e) => e.path !== "fonts/Noto Sans Italic/0-255.pbf");
    assert.throws(() => assetPlan(t2), /Noto Sans Italic: expected 256 glyph ranges, found 255/);
  });
});

describe("manifest", () => {
  const sample = {
    generatedAt: "2026-10-09T10:00:00.000Z",
    profile: "standard",
    build: BUILDS[2],
    sourceUrl: "https://build.protomaps.com/20261009.pmtiles",
    archive: {
      path: "v4/keelson-20261009-standard.pmtiles",
      bytes: 45012345,
      sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      minzoom: 0,
      maxzoom: 6,
    },
    assetsCommit: "028c18f713baecad011301ff7a69acc39bcc2ae7",
  };

  it("deep-equals the fixture the browser's zod schema is tested with (packages/viz)", () => {
    const fixture = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));
    assert.deepEqual(buildManifest(sample), fixture);
    assert.deepEqual(Object.keys(buildManifest(sample)), Object.keys(fixture));
    assert.deepEqual(manifestProblems(fixture), []);
  });

  it("drops a malformed b3sum instead of passing it on", () => {
    assert.equal(buildManifest({ ...sample, build: { ...BUILDS[3] } }).source.b3sum, null);
    assert.equal(buildManifest({ ...sample, build: { ...BUILDS[3], b3sum: "<script>" } }).source.b3sum, null);
  });

  it("--check flags broken manifests", () => {
    const m = buildManifest(sample);
    assert.match(manifestProblems({ ...m, schema: "x" }).join(), /schema/);
    assert.match(manifestProblems({ ...m, archive: { ...m.archive, path: "../x.pmtiles" } }).join(), /archive.path/);
    assert.match(manifestProblems({ ...m, archive: { ...m.archive, path: "v4/{z}.pmtiles" } }).join(), /archive.path/);
    assert.match(manifestProblems({ ...m, source: { ...m.source, basemap_version: "5.0.0" } }).join(), /tiles version/);
    assert.deepEqual(manifestProblems(null), ["manifest is not an object"]);
  });

  it("is written atomically: a failure before the rename leaves no manifest.json", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "basemap-"));
    try {
      const file = path.join(dir, "manifest.json");
      const failing = { ...fs, renameSync: () => { throw new Error("disk full"); } };
      assert.throws(() => writeFileAtomic(file, "{}", failing), /disk full/);
      assert.deepEqual(fs.readdirSync(dir), []);
      writeFileAtomic(file, '{"ok":1}', fs);
      assert.deepEqual(fs.readdirSync(dir), ["manifest.json"]);
      assert.equal(fs.readFileSync(file, "utf8"), '{"ok":1}');
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("prunes older archives, keeping the newest N", () => {
    const names = ["keelson-20261001-standard.pmtiles", "keelson-20261009-lite.pmtiles", "keelson-20261005-detail.pmtiles", "notes.txt", "keelson-20261009-standard.pmtiles"];
    assert.deepEqual(archivesToPrune(names, "keelson-20261009-standard.pmtiles", 1), [
      "keelson-20261009-lite.pmtiles",
      "keelson-20261005-detail.pmtiles",
      "keelson-20261001-standard.pmtiles",
    ]);
    assert.deepEqual(archivesToPrune(names, "keelson-20261009-standard.pmtiles", 2), ["keelson-20261005-detail.pmtiles", "keelson-20261001-standard.pmtiles"]);
  });
});

describe("Natural Earth outline", () => {
  it("rounds, drops repeated points and degenerate rings, closes rings, merges into one MultiPolygon", () => {
    const fc = {
      type: "FeatureCollection",
      features: [
        { type: "Feature", properties: { featurecla: "Land" }, geometry: { type: "Polygon", coordinates: [[[0.001, 0.004], [1.004, 0], [1, 1.002], [0.999, 1.004], [0, 1], [-0.001, 0.001]]] } },
        { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[[5, 5], [5.001, 5.001], [5.002, 5], [5, 5]]] } },
        {
          type: "Feature",
          properties: {},
          geometry: {
            type: "MultiPolygon",
            coordinates: [
              [
                [[10, 10], [12, 10], [12, 12], [10, 12], [10, 10]],
                [[10.5, 10.5], [10.6, 10.5], [10.5, 10.6], [10.5, 10.5]],
              ],
            ],
          },
        },
      ],
    };
    const out = reduceLand(fc, 2);
    assert.equal(out.features.length, 1);
    assert.deepEqual(out.features[0].properties, {});
    const polys = out.features[0].geometry.coordinates;
    assert.equal(polys.length, 2); // the 5,5 sliver collapses
    assert.deepEqual(polys[0], [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]);
    assert.ok(polys[0][0].every(([x, y]) => !Object.is(x, -0) && !Object.is(y, -0)));
    assert.equal(polys[1].length, 2);
    assert.throws(() => reduceLand({ type: "Feature" }), /FeatureCollection/);
  });
});

describe("mapLimit", () => {
  it("keeps at most N tasks in flight and preserves order", async () => {
    let inFlight = 0;
    let peak = 0;
    const out = await mapLimit([1, 2, 3, 4, 5, 6, 7], 3, async (n) => {
      inFlight++;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 5));
      inFlight--;
      return n * 2;
    });
    assert.deepEqual(out, [2, 4, 6, 8, 10, 12, 14]);
    assert.equal(peak, 3);
  });
});
