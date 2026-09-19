/* ==========================================================================
   108 Tools — tools-data.js
   THE REGISTRY. This is the single source of truth for the whole site.

   To add a new tool you do exactly two things:
     1. Create tools/<slug>.html  (copy an existing tool page)
     2. Add one object to the array below

   The homepage grid, the tools page grid, the search, the category chips and
   the "related tools" strips all read from here. You never edit a grid by hand.

   Field meanings:
     slug      -> the file name without .html, and the URL people share
     name      -> shown on the card and as the page <h1>
     desc      -> one line, under 90 characters, plain language
     category  -> must be one of: Text, Image, Calculator, Developer
     icon      -> a single emoji (no image file = nothing to download)
     keywords  -> extra words the search should match; think like a visitor
     popular   -> true shows it on the homepage "Popular tools" grid
   ========================================================================== */

const CATEGORIES = ["Text", "Image", "Calculator", "Developer"];

const TOOLS = [
  {
    slug: "word-counter",
    name: "Word & Character Counter",
    desc: "Count words, characters, sentences and reading time as you type.",
    category: "Text",
    icon: "\u{1F4DD}",
    keywords: ["word count", "character count", "letter counter", "essay length", "reading time"],
    popular: true
  },
  {
    slug: "case-converter",
    name: "Case Converter",
    desc: "Switch text to UPPERCASE, lowercase, Title Case, camelCase and more.",
    category: "Text",
    icon: "\u{1F521}",
    keywords: ["uppercase", "lowercase", "title case", "capitalize", "camelcase", "snake case"],
    popular: true
  },
  {
    slug: "lorem-ipsum-generator",
    name: "Lorem Ipsum Generator",
    desc: "Generate placeholder text by words, sentences or paragraphs.",
    category: "Text",
    icon: "\u{1F4C4}",
    keywords: ["lorem ipsum", "dummy text", "placeholder text", "filler text"],
    popular: false
  },
  {
    slug: "remove-duplicate-lines",
    name: "Remove Duplicate Lines",
    desc: "Delete repeated lines from any list and keep the original order.",
    category: "Text",
    icon: "\u{1F9F9}",
    keywords: ["remove duplicate lines", "delete duplicate lines", "dedupe list", "unique lines", "remove repeated lines"],
    popular: false
  },
  {
    slug: "find-and-replace",
    name: "Find and Replace Text",
    desc: "Swap one word for another everywhere, with regex and whole-word options.",
    category: "Text",
    icon: "\u{1F50D}",
    keywords: ["find and replace", "replace text online", "bulk replace", "search and replace", "regex replace"],
    popular: false
  },
  {
    slug: "sort-text-lines",
    name: "Sort Text Lines",
    desc: "Sort a list A to Z, by number or by length, and drop duplicates.",
    category: "Text",
    icon: "\u{1F523}",
    keywords: ["sort lines", "alphabetical order", "sort list online", "sort text", "az sorter"],
    popular: false
  },
  {
    slug: "remove-line-breaks",
    name: "Remove Line Breaks",
    desc: "Fix text copied from a PDF, or flatten a list into one line.",
    category: "Text",
    icon: "\u{21A9}\u{FE0F}",
    keywords: ["remove line breaks", "remove paragraph breaks", "fix pdf text", "join lines", "remove enter"],
    popular: false
  },
  {
    slug: "whitespace-remover",
    name: "Whitespace Remover",
    desc: "Strip extra spaces, tabs, blank lines and invisible characters.",
    category: "Text",
    icon: "\u{2728}",
    keywords: ["remove extra spaces", "whitespace remover", "trim spaces", "remove tabs", "remove blank lines"],
    popular: false
  },
  {
    slug: "reverse-text",
    name: "Reverse Text",
    desc: "Flip text backwards, or reverse the order of the words and lines.",
    category: "Text",
    icon: "\u{23EA}",
    keywords: ["reverse text", "backwards text", "flip text", "reverse words", "mirror text", "reverse string"],
    popular: false
  },
  {
    slug: "text-repeater",
    name: "Text Repeater",
    desc: "Repeat any word, line or block of text as many times as you want.",
    category: "Text",
    icon: "\u{1F501}",
    keywords: ["text repeater", "repeat text", "copy text multiple times", "duplicate text", "repeat word"],
    popular: false
  },
  {
    slug: "add-line-numbers",
    name: "Add Line Numbers",
    desc: "Number every line, with your own start, step, separator and padding.",
    category: "Text",
    icon: "\u{1F522}",
    keywords: ["add line numbers", "number lines", "line numbering", "numbered list", "renumber lines"],
    popular: false
  },
  {
    slug: "slug-generator",
    name: "URL Slug Generator",
    desc: "Turn a title into a clean lowercase URL slug, one per line.",
    category: "Text",
    icon: "\u{1F517}",
    keywords: ["slug generator", "url slug", "permalink generator", "seo friendly url", "slugify"],
    popular: false
  },
  {
    slug: "character-frequency-counter",
    name: "Character Frequency Counter",
    desc: "See how often each character or word appears, ranked with percentages.",
    category: "Text",
    icon: "\u{1F520}",
    keywords: ["character frequency", "letter frequency", "count letters", "word frequency", "letter counter"],
    popular: false
  },
  {
    slug: "image-compressor",
    name: "Image Compressor",
    desc: "Reduce JPG and PNG file size in your browser. Nothing is uploaded.",
    category: "Image",
    icon: "\u{1F5BC}️",
    keywords: ["compress image", "reduce image size", "shrink jpg", "optimize png"],
    popular: true
  },
  {
    slug: "image-converter",
    name: "Image Format Converter",
    desc: "Convert between PNG, JPG and WebP without leaving the page.",
    category: "Image",
    icon: "\u{1F504}",
    keywords: ["png to jpg", "jpg to png", "webp converter", "change image format"],
    popular: true
  },
  {
    slug: "percentage-calculator",
    name: "Percentage Calculator",
    desc: "Work out X% of Y, what percent one number is of another, and change.",
    category: "Calculator",
    icon: "\u{1F4CA}",
    keywords: ["percentage", "percent of", "percent increase", "percent decrease", "discount"],
    popular: true
  },
  {
    slug: "age-calculator",
    name: "Age Calculator",
    desc: "Exact age in years, months and days, plus your next birthday.",
    category: "Calculator",
    icon: "\u{1F382}",
    keywords: ["age calculator", "how old am i", "date of birth", "birthday countdown"],
    popular: true
  },
  {
    slug: "emi-calculator",
    name: "EMI / Loan Calculator",
    desc: "Monthly instalment, total interest and a full repayment schedule.",
    category: "Calculator",
    icon: "\u{1F3E6}",
    keywords: ["emi calculator", "loan calculator", "home loan", "car loan", "interest"],
    popular: false
  },
  {
    slug: "bmi-calculator",
    name: "BMI Calculator",
    desc: "Body Mass Index from your height and weight, with the healthy range.",
    category: "Calculator",
    icon: "\u{2696}\u{FE0F}",
    keywords: ["bmi calculator", "body mass index", "healthy weight", "bmi chart", "ideal weight"],
    popular: false
  },
  {
    slug: "discount-calculator",
    name: "Discount Calculator",
    desc: "Sale price, what you save, and what stacked offers really come to.",
    category: "Calculator",
    icon: "\u{1F3F7}\u{FE0F}",
    keywords: ["discount calculator", "sale price", "percent off", "how much do i save", "original price"],
    popular: false
  },
  {
    slug: "tip-calculator",
    name: "Tip Calculator",
    desc: "Work out the tip and split the bill between any number of people.",
    category: "Calculator",
    icon: "\u{1F37D}\u{FE0F}",
    keywords: ["tip calculator", "split the bill", "how much to tip", "service charge", "bill split"],
    popular: false
  },
  {
    slug: "average-calculator",
    name: "Average Calculator",
    desc: "Mean, median, mode, range and standard deviation from any list.",
    category: "Calculator",
    icon: "\u{1F4C8}",
    keywords: ["average calculator", "mean median mode", "find the average", "standard deviation", "range"],
    popular: false
  },
  {
    slug: "ratio-calculator",
    name: "Ratio Calculator",
    desc: "Simplify a ratio, solve a proportion, or split an amount by ratio.",
    category: "Calculator",
    icon: "\u{2797}",
    keywords: ["ratio calculator", "simplify ratio", "proportion", "aspect ratio", "divide in ratio"],
    popular: false
  },
  {
    slug: "password-generator",
    name: "Password Generator",
    desc: "Create strong random passwords with a live strength meter.",
    category: "Developer",
    icon: "\u{1F512}",
    keywords: ["password generator", "strong password", "random password", "secure password"],
    popular: true
  },
  {
    slug: "json-formatter",
    name: "JSON Formatter & Validator",
    desc: "Beautify or minify JSON and see exactly where the error is.",
    category: "Developer",
    icon: "\u{1F9E9}",
    keywords: ["json formatter", "json beautifier", "json validator", "pretty print json", "minify json"],
    popular: true
  }
];
