/**
 * parseBookMeta — unit tests against REAL filenames from the authentic_library
 * corpus (pulled from Mongo documents.filename, 2026-07-05).
 *
 * Run (Node ≥ 22.6 strips types natively):
 *   node tests/unit/book-meta.test.ts
 * Non-zero exit on any failure — safe for CI / assert-before-commit.
 */
import { strict as assert } from "node:assert";
import { parseBookMeta } from "../../src/lib/label-utils.ts";

type Case = {
  name: string;
  raw: string;
  title: string;
  author?: string;
  year?: string;
  series?: string;
};

const CASES: Case[] = [
  {
    name: "libgen author-first with degree noise + underscore colon",
    raw: "(Hons), Gene Da Rocha Msc Bsc - Learning sqlite for iOS_ extend SQLite with mobile development skills to build great apps for iOS devices (2016, Packt Publishing Limited).md",
    title:
      "Learning sqlite for iOS: extend SQLite with mobile development skills to build great apps for iOS devices",
    author: "Gene Da Rocha",
    year: "2016",
  },
  {
    name: "libgen series + Last,First flip",
    raw: "[Roundtable series in behavioral economics] Camerer, Colin F - Behavioral Game Theory_ Experiments in Strategic Interaction (2011, Princeton University Press).md",
    title: "Behavioral Game Theory: Experiments in Strategic Interaction",
    author: "Colin F Camerer",
    year: "2011",
    series: "Roundtable series in behavioral economics",
  },
  {
    name: "Anna's Archive double-dash with hash tail",
    raw: "The Denial of Death -- Ernest Becker [Becker, Ernest] -- 1977 -- Simon and Schuster -- dc49150836d49c61a56bdb56b1c7602e -- Anna's Archive.md",
    title: "The Denial of Death",
    author: "Ernest Becker",
    year: "1977",
  },
  {
    name: "snake_case fallback",
    raw: "Bayesian_Reasoning_and_Machine_Learning.md",
    title: "Bayesian Reasoning and Machine Learning",
    author: "",
    year: "",
  },
  {
    name: "short Title - Author stays title-first",
    raw: "Mastery - Robert Greene.md",
    title: "Mastery",
    author: "Robert Greene",
  },
  {
    name: "long title - author stays title-first",
    raw: "100 Things Every Designer Needs to Know About People - Susan Weinschenk.md",
    title: "100 Things Every Designer Needs to Know About People",
    author: "Susan Weinschenk",
  },
  {
    name: "libgen underscore-joined co-authors",
    raw: "[Community experience distilled] Kapadia, Amar_Rajana, Kris_Varma, Sreedhar - OpenStack object storage (Swift) essentials design, implement, and successfully manage your own object storage cluster (2015, Packt Publishing).md",
    title:
      "OpenStack object storage (Swift) essentials design, implement, and successfully manage your own object storage cluster",
    author: "Amar Kapadia",
    year: "2015",
  },
  {
    name: "author-first via year paren, no comma in author",
    raw: "[Collected Works of C.G. Jung] Carl Jung - Collected Works of C.G. Jung_ General Index 20 (1979).md",
    title: "Collected Works of C.G. Jung: General Index 20",
    author: "Carl Jung",
    year: "1979",
    series: "Collected Works of C.G. Jung",
  },
  {
    name: "two-author First Last list keeps first (no bogus flip)",
    raw: "[Advances in Game-Based Learning ] Scott J. Warren, Greg Jones (auth.) - Learning Games_ The Science and Art of Development (2017, Springer).md",
    title: "Learning Games: The Science and Art of Development",
    author: "Scott J. Warren",
    year: "2017",
  },
  {
    name: "chase hughes — plain author-first with year+publisher",
    raw: "Chase Hughes - The Ellipsis Manual_ analysis and engineering of human behavior (2017, Evergreen Press).md",
    title: "The Ellipsis Manual: analysis and engineering of human behavior",
    author: "Chase Hughes",
    year: "2017",
  },
  {
    name: "coll_ collective-author marker",
    raw: "coll_Standards_for_educational_and_psychological_testing_201.md",
    title: "Standards for educational and psychological testing 201",
    author: "",
  },
  {
    name: "ordinal note file",
    raw: "12_flutter_local_llms_privacy.md",
    title: "12 flutter local llms privacy",
    author: "",
  },
  {
    name: "series + Last,First single-word surname flips",
    raw: "[Game Theory 101 ] Spaniel, William - The Complete Textbook (2014).md",
    title: "The Complete Textbook",
    author: "William Spaniel",
    year: "2014",
    series: "Game Theory 101",
  },
  {
    name: "embedded {Author} braces, no dash separator",
    raw: "[Handbook of the Uncertain Self] Handbook of the Uncertain Self __ Human Groups, Social Categories, and Collective Self_ Social Id...{Hogg, Michael A.}.md",
    title:
      "Handbook of the Uncertain Self: Human Groups, Social Categories, and Collective Self: Social Id",
    author: "Michael A. Hogg",
  },
  {
    name: "editor annotation stripped from author",
    raw: "[Advancing Theory in Therapy] Windy Dryden (editor) - Rational Emotive Behaviour Therapy (2003, Routledge).md",
    title: "Rational Emotive Behaviour Therapy",
    author: "Windy Dryden",
    year: "2003",
  },
  {
    name: "empty / null-ish input",
    raw: "",
    title: "",
    author: "",
  },
  // ── kebab-slug convention (authentic_library_v2 source files) ──
  {
    name: "kebab slug with year + publisher tail",
    raw: "bird-by-bird-some-instructions-on-writing-and-life-anne-lamott-2007-anchor-books",
    title: "Bird by Bird Some Instructions on Writing and Life Anne Lamott",
    author: "",
    year: "2007",
  },
  {
    name: "kebab slug, no year",
    raw: "atomic-habits-james-clear.md",
    title: "Atomic Habits James Clear",
    author: "",
    year: "",
  },
  {
    name: "kebab slug leading small word capitalized",
    raw: "a-philosophy-of-software-design-john-ousterhout.md",
    title: "A Philosophy of Software Design John Ousterhout",
  },
  {
    name: "kebab slug with hex hash tail",
    raw: "oreillymedia-7f0db46e2a.md",
    title: "Oreillymedia",
  },
  {
    name: "stacked extensions from converted files",
    raw: "Data Distillation for Efficient and Faithful.pdf.md",
    title: "Data Distillation for Efficient and Faithful",
    author: "",
  },
  {
    name: "irregular dash spacing still splits author-first",
    raw: "Daniel Shiffman  -  The Nature of Code Simulating Natural Systems with Processing.md",
    title: "The Nature of Code Simulating Natural Systems with Processing",
    author: "Daniel Shiffman",
  },
  {
    name: "en-dash separator splits",
    raw: "Robert Greene – The 48 Laws of Power (2000, Penguin).md",
    title: "The 48 Laws of Power",
    author: "Robert Greene",
    year: "2000",
  },
];

let failed = 0;
for (const c of CASES) {
  try {
    const m = parseBookMeta(c.raw);
    assert.equal(m.title, c.title, `title mismatch: got "${m.title}"`);
    if (c.author !== undefined)
      assert.equal(m.author, c.author, `author mismatch: got "${m.author}"`);
    if (c.year !== undefined)
      assert.equal(m.year, c.year, `year mismatch: got "${m.year}"`);
    if (c.series !== undefined)
      assert.equal(m.series, c.series, `series mismatch: got "${m.series}"`);
    assert.equal(m.raw, c.raw, "raw must be preserved verbatim");
    console.log(`PASS ${c.name}`);
  } catch (err) {
    failed += 1;
    console.error(`FAIL ${c.name}\n  raw: ${c.raw.slice(0, 100)}\n  ${(err as Error).message}`);
  }
}

console.log(`\n${CASES.length - failed}/${CASES.length} passed`);
process.exit(failed ? 1 : 0);
