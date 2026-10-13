# tacit-knowledge

[English](README.md)

tacit-knowledgeは、Claude CodeとCodexで使うスキルです。自分の会話履歴から、判断基準、こだわり、繰り返す作業手順、過去の失敗を書き出します。各項目には、自分が打った言葉を日時とsessionを添えてそのまま引用するので、引用が履歴に本当にあるかを同梱のスクリプトで照合できる仕組みです。

エージェントに伝えた訂正（「まず公式ドキュメントで確かめて」「CIが通るまでmergeしないで」）は、すでに言葉になったドメイン知識ですが、多くのsessionに散らばっています。このスキルはそれを集め、AIが書いた要約と本人の言葉を分けて残します。そのため、どの行が履歴から来たもので、どの行をAIが足したのかを利用者が見分けられる形です。

## 仕組み

1. `extract.py` が1つのプロジェクトの履歴を読みます。対話のsessionで人が打った発話だけを残し、発話ごとに見出しを付けてテキストファイルに書き出します。
2. エージェントがそのファイルを読み、Markdownの文書を書きます。各項目の下には、本人の発話を引用する根拠行が付きます。
3. `verify.py` がすべての根拠行を履歴と照合します。履歴にない引用と、根拠行のない項目を検出すると失敗を返します。

根拠行は次の形です。

```markdown
- 採否はこの環境での実測で決め、記事の主張では決めません。
  > 2026-03-02 23:10+09:00 3f2a9c1e-7b4「記事の数字ではなく、ここで測った結果で決めてください」
```

スクリプトはPythonの標準ライブラリだけを使い、モデルを呼び出しません。要約は、利用者がふだん使っているエージェントが書きます。

## 必要なもの

- Python 3.9以上
- Claude CodeとCodexのどちらか、または両方

## 導入

### Claude Code

```sh
claude plugin marketplace add okamyuji/tacit-knowledge
claude plugin install tacit-knowledge@tacit-knowledge
```

このpluginに入っているのはこのスキル1つだけで、hookは含みません。

### Codex

Codexは、ユーザー単位のスキルを `~/.codex/skills/<名前>/SKILL.md` から読み込みます（[Codexのスキル](https://developers.openai.com/blog/eval-skills)）。`CODEX_HOME` を設定している場合は、`~/.codex` の代わりにそのディレクトリを使ってください。

```sh
git clone https://github.com/okamyuji/tacit-knowledge.git
mkdir -p ~/.codex/skills
cp -R tacit-knowledge/skills/tacit-knowledge ~/.codex/skills/
```

## 使い方

履歴を読みたいプロジェクトを開き、エージェントに次のように頼みます。

> このプロジェクトの履歴から、私の判断基準、こだわり、作業手順を抽出してください。

エージェントは出力先を確認し、抽出、文書の作成、照合を進めます。照合は、すべての行が通るまで繰り返すのが手順です。

スクリプトを直接実行することもできます。

```sh
python3 skills/tacit-knowledge/scripts/extract.py --out ~/notes/tacit --project ~/work/my-app
python3 skills/tacit-knowledge/scripts/verify.py ~/notes/tacit/tacit-knowledge.md --project ~/work/my-app
```

| オプション | 意味 |
|---|---|
| `--project PATH` | 対象のプロジェクトのディレクトリです。既定値はカレントディレクトリです。 |
| `--source claude,codex` | 読む履歴の種類です。既定では両方を読みます。 |
| `--tz ZONE` | 見出しに使うIANAのタイムゾーンです（例 `Asia/Tokyo`）。既定値は実行環境のタイムゾーンです。 |
| `--chunk-chars N` | `extract.py` が書く1ファイルあたりの最大文字数です。既定値は20000です。 |
| `--include-non-interactive` | 非対話のsessionも読みます（次の節を参照）。両方のスクリプトに付けてください。 |

終了コードは、成功で0、`verify.py` が照合に失敗した行を見つけたときに1、履歴が見つからないときや引数が誤っているときに2を返します。

## 対象外のもの

- ツールが利用者の発話として書き込む文です。system-reminder、コマンドの出力、Codexのenvironment_contextなどが当たります。
- 非対話のsessionです。`claude -p` やSDKからの実行、`codex exec`、サブエージェントが当たります。これらの発話は、たいてい別のエージェントが書いています。`claude -p` や `codex exec` に自分で指示を打ち込んでいる場合は、`extract.py` と `verify.py` の両方に `--include-non-interactive` を付けてください。
- Claude Codeが削除済みの履歴です。Claude Codeは `cleanupPeriodDays` の日数（既定値は30日）を過ぎた履歴を削除します（[`cleanupPeriodDays`](https://code.claude.com/docs/en/settings-reference#cleanupperioddays)）。長く残したい場合は、`~/.claude/settings.json` で `"cleanupPeriodDays": 365` のように値を増やしてください。

## 限界

`verify.py` が確かめるのは、引用がその日時のそのsessionに実在することです。項目の要約が引用と同じ意味かどうかは確かめません。項目を判断の拠り所にする前に、要約と引用を読み比べてください。

## 取り扱いの注意

抽出したファイルと文書には、利用者が打った発話がそのまま入るので、秘密情報や顧客の情報が混ざっている場合もあります。`extract.py` はファイルを権限 `0600` で書き、新しく作る出力ディレクトリを権限 `0700` にします。出力はリポジトリの外に置き、共有しないでください。

## 開発

```sh
python3 -m unittest discover -s tests -v
```

## ライセンス

[MIT](LICENSE)
