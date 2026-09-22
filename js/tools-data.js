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
     category  -> must be one of the CATEGORIES below; check.py enforces it
     icon      -> a single emoji (no image file = nothing to download)
     keywords  -> extra words the search should match; think like a visitor
     popular   -> true shows it on the homepage "Popular tools" grid
   ========================================================================== */

/* The plan, not the inventory. A category may sit here with nothing in it
   yet - main.js only draws a chip once at least one tool claims the category,
   so this list can run ahead of the build without showing an empty grid. */
const CATEGORIES = ["Text", "Image", "Calculator", "Developer",
                    "Converter", "PDF", "Date & Time", "Random"];

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
    slug: "gst-calculator",
    name: "GST Calculator",
    desc: "Add GST to a price or strip it out, with the CGST, SGST and IGST split.",
    category: "Calculator",
    icon: "\u{1F9FE}",
    keywords: ["gst calculator", "gst india", "reverse gst", "remove gst", "cgst sgst igst", "gst inclusive", "add gst", "tax calculator"],
    popular: true
  },
  {
    slug: "salary-calculator",
    name: "Salary Calculator",
    desc: "Turn an annual CTC into your real monthly in-hand, component by component.",
    category: "Calculator",
    icon: "\u{1F4B8}",
    keywords: ["salary calculator", "ctc to in hand", "take home salary", "in hand salary", "ctc calculator", "gross to net salary", "salary breakup", "basic hra pf"],
    popular: false
  },
  {
    slug: "fuel-cost-calculator",
    name: "Fuel Cost Calculator",
    desc: "What a trip costs in petrol or diesel, split per person and per month.",
    category: "Calculator",
    icon: "\u{26FD}",
    keywords: ["fuel cost calculator", "petrol cost", "diesel cost", "trip cost calculator", "mileage cost", "fuel price per km", "commute cost"],
    popular: false
  },
  {
    slug: "simple-interest-calculator",
    name: "Simple Interest Calculator",
    desc: "Interest on the principal only, with the total repayable and a yearly table.",
    category: "Calculator",
    icon: "\u{1F4B0}",
    keywords: ["simple interest", "si calculator", "interest calculator", "principal rate time", "flat rate loan"],
    popular: false
  },
  {
    slug: "compound-interest-calculator",
    name: "Compound Interest Calculator",
    desc: "See what a lump sum grows to, and how far ahead of simple interest it ends up.",
    category: "Calculator",
    icon: "\u{1F331}",
    keywords: ["compound interest", "ci calculator", "compounding", "rule of 72", "future value", "fd maturity"],
    popular: true
  },
  {
    slug: "fraction-calculator",
    name: "Fraction Calculator",
    desc: "Add, subtract, multiply or divide fractions and see every step of the working.",
    category: "Calculator",
    icon: "\u{1F9EE}",
    keywords: ["fraction calculator", "add fractions", "simplify fraction", "mixed number", "lowest terms", "improper fraction"],
    popular: false
  },
  {
    slug: "margin-markup-calculator",
    name: "Margin & Markup Calculator",
    desc: "Set a price from cost, or find the real margin and markup on what you sell.",
    category: "Calculator",
    icon: "\u{1F4B9}",
    keywords: ["margin calculator", "markup calculator", "profit margin", "selling price", "gross margin", "cost price"],
    popular: false
  },
  {
    slug: "area-converter",
    name: "Area Converter",
    desc: "Square feet, acres, hectares and the Indian units - guntha, cent, marla, bigha.",
    category: "Converter",
    icon: "\u{1F4D0}",
    keywords: ["area converter", "square feet to square metre", "guntha", "cent to sqft", "bigha", "marla", "kanal", "gaj", "land area"],
    popular: true
  },
  {
    slug: "number-to-words",
    name: "Number to Words",
    desc: "Spell any number in lakh and crore or million and billion, ready for a cheque.",
    category: "Converter",
    icon: "\u{1F4D6}",
    keywords: ["number to words", "rupees in words", "lakh crore", "amount in words", "cheque writing", "spell number"],
    popular: true
  },
  {
    slug: "temperature-converter",
    name: "Temperature Converter",
    desc: "Celsius, Fahrenheit, Kelvin and Rankine together, with the formula shown.",
    category: "Converter",
    icon: "\u{1F321}\u{FE0F}",
    keywords: ["temperature converter", "celsius to fahrenheit", "fahrenheit to celsius", "kelvin", "c to f"],
    popular: false
  },
  {
    slug: "text-to-morse",
    name: "Morse Code Translator",
    desc: "Text to Morse and Morse back to text, punctuation included.",
    category: "Converter",
    icon: "\u{1F4FB}",
    keywords: ["morse code", "text to morse", "morse to text", "morse translator", "morse decoder", "dots and dashes", "sos"],
    popular: false
  },
  {
    slug: "nato-phonetic-converter",
    name: "NATO Phonetic Alphabet",
    desc: "Spell a name or reference out loud so it cannot be misheard.",
    category: "Converter",
    icon: "\u{1F4DE}",
    keywords: ["nato phonetic alphabet", "phonetic alphabet", "alfa bravo charlie", "spell on phone", "military alphabet", "aviation alphabet"],
    popular: false
  },
  {
    slug: "date-difference-calculator",
    name: "Date Difference Calculator",
    desc: "Days, weeks, months and working days between any two dates.",
    category: "Date & Time",
    icon: "\u{1F4C5}",
    keywords: ["date difference", "days between dates", "how many days", "date calculator", "working days"],
    popular: false
  },
  {
    slug: "add-subtract-days",
    name: "Add or Subtract Days",
    desc: "Move a date by days, weeks, months, years or working days.",
    category: "Date & Time",
    icon: "\u{1F5D3}\u{FE0F}",
    keywords: ["add days to date", "subtract days", "date plus days", "deadline calculator", "notice period"],
    popular: false
  },
  {
    slug: "sip-calculator",
    name: "SIP Calculator",
    desc: "What a monthly investment grows to, with an annual step-up option.",
    category: "Calculator",
    icon: "\u{1F4B5}",
    keywords: ["sip calculator", "mutual fund calculator", "systematic investment plan", "step up sip", "monthly investment", "sip returns"],
    popular: true
  },
  {
    slug: "unit-converter",
    name: "Unit Converter",
    desc: "Length, weight and volume between metric and imperial, all units at once.",
    category: "Converter",
    icon: "\u{1F4CF}",
    keywords: ["unit converter", "length converter", "weight converter", "volume converter", "cm to inches", "kg to pounds", "litres to gallons"],
    popular: true
  },
  {
    slug: "binary-decimal-hex-converter",
    name: "Binary, Decimal and Hex",
    desc: "Convert between base 2, 8, 10 and 16, exactly, however long the number.",
    category: "Converter",
    icon: "\u{1F5A5}\u{FE0F}",
    keywords: ["binary to decimal", "decimal to binary", "hex converter", "base converter", "octal", "hexadecimal"],
    popular: false
  },
  {
    slug: "timestamp-converter",
    name: "Unix Timestamp Converter",
    desc: "Epoch to a readable date and back, with seconds and milliseconds told apart.",
    category: "Converter",
    icon: "\u{23F1}\u{FE0F}",
    keywords: ["unix timestamp", "epoch converter", "timestamp to date", "date to timestamp", "epoch time"],
    popular: false
  },
  {
    slug: "days-until-countdown",
    name: "Days Until Countdown",
    desc: "How many days until a date, with a live countdown to the exact moment.",
    category: "Date & Time",
    icon: "\u{23F3}",
    keywords: ["days until", "countdown", "how many days until", "days since", "countdown timer", "days left"],
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
  },
  {
    slug: "roman-numeral-converter",
    name: "Roman Numeral Converter",
    desc: "Numbers to Roman numerals and back, with the sum shown and bad spellings caught.",
    category: "Converter",
    icon: "\u{1F3DB}\u{FE0F}",
    keywords: ["roman numerals", "roman numeral converter", "number to roman", "roman to number", "mcmxciv", "roman numeral date"],
    popular: false
  },
  {
    slug: "data-storage-converter",
    name: "Data Storage Converter",
    desc: "Bytes, KB, MB, GB and TB, with the 1024-based units kept separate.",
    category: "Converter",
    icon: "\u{1F4BE}",
    keywords: ["kb to mb", "mb to gb", "gb to tb", "bytes converter", "file size converter", "kib vs kb", "why is my 1tb drive 931gb"],
    popular: false
  },
  {
    slug: "speed-converter",
    name: "Speed Converter",
    desc: "km/h, mph, m/s, knots and feet per second, plus the same speed as a running pace.",
    category: "Converter",
    icon: "\u{1F3CE}\u{FE0F}",
    keywords: ["kmh to mph", "mph to kmh", "m/s to km/h", "knots to mph", "running pace", "min per km", "pace converter"],
    popular: false
  },
  {
    slug: "leap-year-checker",
    name: "Leap Year Checker",
    desc: "Is it a leap year? The answer, and which of the three rules decided it.",
    category: "Date & Time",
    icon: "\u{1F4C6}",
    keywords: ["leap year", "is 2026 a leap year", "leap year checker", "29 february", "leap year rule", "days in february"],
    popular: false
  },
  {
    slug: "week-number-calculator",
    name: "Week Number Calculator",
    desc: "The ISO 8601 week number for any date, and the dates any week covers.",
    category: "Date & Time",
    icon: "\u{1F5D3}\u{FE0F}",
    keywords: ["week number", "iso week", "what week is it", "current week number", "week number calculator", "week of year"],
    popular: false
  }
];
