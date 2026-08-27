# Spec de implementação — Agente de Reembolso

## 1. Objetivo e escopo

Implementar, no repositório fornecido, um agente conversacional de reembolso que:

- atenda conversas de quatro a nove turnos sem depender de o cliente reenviar o histórico;
- aceite texto e anexo opcional em qualquer turno e em qualquer ordem;
- use um supervisor e três subagentes, orquestrados por grafo com handoff explícito;
- consulte a base normativa por busca híbrida e a operadora pelas três ferramentas MCP fornecidas;
- produza toda resposta HTTP no modelo Pydantic `ChatResponse` existente;
- aplique regras de elegibilidade e cálculo de modo determinístico sempre que possível;
- preserve privacidade, isolamento entre sessões e o vínculo com a carteirinha que abriu a sessão;
- seja entregue com o índice persistido em `storage/` e executável no contrato Docker da banca.

Ficam fora do escopo: front-end, deploy em nuvem, tracing, observabilidade, relatórios, documentação adicional e uma suíte própria de evals automatizadas. Os casos de treino fornecidos serão usados apenas como validação funcional.

## 2. Requisitos obrigatórios

### 2.1 Arquitetura

O fluxo será um `StateGraph` do LangGraph, compilado com checkpointer e chaveado pelo `session_id`. Não haverá roteamento central implementado somente com `if/elif`.

```text
POST /chat
    |
    v
Supervisor <-----------------------------------+
    |                                          |
    +-- handoff --> Subagente de Triagem ------+
    +-- handoff --> Subagente de Documento ----+
    +-- handoff --> Subagente de Normas --------+
    |
    +-- resposta final estruturada
```

Responsabilidades:

- **Supervisor:** interpreta a necessidade atual, protege a continuidade da conversa, escolhe o próximo subagente, coordena MCP e consolida o `ChatResponse`.
- **Triagem:** identifica intenção, solicita a carteirinha quando ausente, consulta beneficiário e histórico, verifica contrato e controla pendências cadastrais.
- **Documento:** decodifica e extrai anexos, classifica a categoria e captura apenas fatos cuja fonte autorizada é o documento.
- **Normas:** recupera dispositivos da base, resolve vigência e precedência normativa e entrega regras aplicáveis com evidências.

Cada handoff será representado no estado e como transição/nó nomeado no grafo. O supervisor poderá receber o controle de volta após cada subagente e encaminhar o mesmo turno a outro subagente quando a resposta exigir mais de uma especialidade.

### 2.2 Estado por sessão

O estado tipado do grafo deverá guardar, no mínimo:

- mensagens da conversa;
- `session_id` e carteirinha vinculada à sessão;
- intenção e pergunta atual;
- anexos recebidos e resultados estruturados de extração;
- dados do beneficiário e histórico obtidos por MCP;
- categoria, pendências e fatos documentais confirmados;
- trechos normativos recuperados e regras efetivamente aplicadas;
- decisão, valores e protocolo;
- último handoff e próxima ação.

O `session_id` será passado ao checkpointer como `thread_id`. O primeiro número de carteirinha validado pelo MCP será imutável durante a sessão. `POST /reset` limpará checkpointer, caches e vínculos de todas as sessões. Estado de uma sessão nunca poderá ser lido por outra.

### 2.3 Matriz de autoridade das fontes

| Informação | Única fonte autorizada | Conduta diante de conflito |
|---|---|---|
| Carteirinha da sessão | Beneficiário | Usar o primeiro identificador validado; recusar troca ou consulta de terceiro |
| Plano, adesão e situação contratual | `consultar_beneficiario` | Ignorar afirmação divergente do beneficiário |
| Sessões realizadas e valor já reembolsado | MCP, incluindo soma de `consultar_historico` | Nunca aceitar estimativa do beneficiário |
| Categoria, valor pago, data, procedimento e campos obrigatórios | Documento anexado | Nunca completar com memória ou relato do beneficiário |
| Cobertura, prazos, limites, coparticipação, alçada e dispositivos | Base normativa recuperada | Aplicar vigência e norma substitutiva mais recente |
| Protocolo | `abrir_protocolo` | Nunca fabricar ou antecipar número |

O estado manterá a proveniência dos fatos relevantes para impedir que uma resposta posterior substitua silenciosamente a fonte correta.

### 2.4 Contrato HTTP e configuração

- `GET /health`: retorna HTTP 200 em até 60 segundos do start, sem construir índice nem depender de uma chamada ao LLM/MCP.
- `POST /chat`: recebe `ChatRequest` e sempre devolve `ChatResponse` válido.
- `POST /reset`: remove todo estado conversacional e retorna confirmação.
- Porta: `8000`.
- Configuração permitida: `BOOTCAMP_LLM_ENDPOINT`, `BOOTCAMP_API_KEY`, `MCP_OPERADORA_URL` e `MCP_OPERADORA_TOKEN`.
- Nenhuma URL, token, chave ou dado de beneficiário será fixado no código.

Campos de decisão permanecerão `null` enquanto não houver base suficiente. `regras_aplicadas` conterá somente identificadores existentes na base e efetivamente usados no desfecho.

## 3. Plano de implementação em cinco etapas

### Etapa 1 — Fundação: contratos, estado e esqueleto do grafo

**Objetivo:** disponibilizar o ciclo HTTP e a memória por sessão antes de incluir raciocínio de negócio.

Entregas:

1. Preservar os enums e modelos públicos de `app/schemas.py` e criar modelos Pydantic internos para documento, MCP, evidência normativa, pendência e resultado dos subagentes.
2. Definir o estado tipado e reducers necessários para mensagens, anexos e evidências.
3. Montar o `StateGraph` com nós separados para supervisor, triagem, documento e normas, incluindo handoffs explícitos e retorno ao supervisor.
4. Compilar o grafo com checkpointer por `thread_id=session_id`.
5. Conectar `/chat` ao grafo e implementar `/reset` com limpeza integral das sessões.
6. Tratar entrada fora de ordem: mensagem vazia com anexo, anexo tardio e múltiplos anexos na mesma sessão.

Critérios de aceite:

- duas sessões simultâneas não compartilham carteirinha, mensagens ou anexos;
- um segundo turno recupera o contexto sem histórico reenviado;
- o grafo evidencia supervisor, três subagentes e transições de handoff;
- `/health`, `/chat` e `/reset` respeitam seus modelos e códigos HTTP.

### Etapa 2 — Ingestão offline e recuperação híbrida

**Objetivo:** construir uma base normativa persistente, pequena o bastante para o Docker e precisa o bastante para citar somente regras aplicáveis.

Entregas:

1. Implementar `python -m ingest.build` com LlamaIndex para ler PDF e DOCX de `kb/`.
2. Extrair texto preservando metadados: arquivo, página/seção, identificador do dispositivo, data de publicação, vigência e referências de revogação/alteração.
3. Criar chunks orientados a dispositivos normativos, com sobreposição apenas quando necessária para manter contexto.
4. Gerar embeddings e persistir um `SimpleVectorStore` em `storage/`.
5. Persistir o corpus lexical necessário ao BM25 no mesmo diretório.
6. Implementar recuperação híbrida: candidatos densos + BM25, fusão por Reciprocal Rank Fusion e reranking final por relevância, vigência e especificidade.
7. Limitar quantidade e tamanho dos trechos enviados ao modelo, mantendo ampla margem abaixo de 50 mil tokens.
8. Rodar a ingestão fora do container e versionar todos os artefatos necessários em `storage/`.

Critérios de aceite:

- o serviço carrega `storage/` sem reindexar ou chamar embeddings no start;
- consultas dos casos de treino recuperam os dispositivos esperados, inclusive circulares substitutivas;
- todo identificador retornado em `regras_aplicadas` pode ser localizado na base;
- a recuperação não envia o corpus completo ao LLM.

### Etapa 3 — Subagentes, anexos e motor determinístico

**Objetivo:** transformar fontes autorizadas em fatos confiáveis e calcular o desfecho sem delegar aritmética ou regras exatas ao modelo.

Entregas:

1. **Triagem:** cliente MCP assíncrono via streamable HTTP, configurado somente pelas variáveis de ambiente; adapters Pydantic tolerantes aos esquemas v1 e v2 de sessões.
2. Consultar beneficiário após obter a carteirinha e consultar histórico quando sessões ou saldo anual forem relevantes.
3. **Documento:** validar base64, MIME e tamanho; extrair texto de PDF, imagem e DOCX; usar OCR em imagem/documento digitalizado; produzir saída Pydantic com categoria, valor, data, procedimento, campos presentes e ausentes.
4. Distinguir `INVALIDO`, `DESPESA_NAO_COBERTA` e documento válido incompleto. Anexo inválido não encerra a sessão nem apaga documentos anteriores.
5. **Normas:** consultar o retriever híbrido, responder perguntas abertas e retornar evidências estruturadas, sem criar dispositivos.
6. Criar funções puras para datas e prazos, teto por procedimento, coparticipação, limite anual, sessões acumuladas, arredondamento monetário com `Decimal`, competência automática e composição da decisão.
7. Garantir que coparticipação isolada resulte em `APROVADO`; `APROVADO_PARCIAL` será usado quando o teto/limite cortar o valor elegível.

Critérios de aceite:

- valores monetários fecham em centavos e não usam `float` no cálculo;
- informação verbal não sobrescreve documento ou MCP;
- documento complementar resolve uma pendência em turno posterior;
- OPME e valor acima da alçada seguem para análise humana sem cálculo de reembolso;
- indisponibilidade ou resposta inválida de dependência gera atendimento útil e pendência segura, nunca resposta vazia.

### Etapa 4 — Política do supervisor, guardrails e resposta conversacional

**Objetivo:** coordenar os componentes em uma conversa natural, segura e correta em todos os turnos.

Entregas:

1. Implementar a política de roteamento sem exigir ordem fixa entre intenção, carteirinha e documento.
2. Responder primeiro à pergunta concreta do turno e, depois, pedir somente a próxima informação realmente necessária.
3. Consolidar o resultado em `ChatResponse`, deixando campos decisórios nulos antes do momento apropriado.
4. Abrir protocolo exatamente uma vez quando houver categoria sempre humana ou valor acima da alçada; retornar `ESCALADO_ANALISTA`, protocolo real e `valor_reembolso_brl=null`.
5. Aplicar guardrail de terceiro: qualquer pedido sobre outra carteirinha recebe `FORA_DE_ESCOPO`, sem ecoar o número e sem abandonar o processo do titular da sessão.
6. Sanitizar texto de entrada, extração, evidências e saída para nunca revelar CPF completo, CID ou hipótese diagnóstica.
7. Gerar respostas contextualizadas com mais de 20 caracteres e evitar repetição literal entre turnos, sem depender de frases exatas dos roteiros de treino.
8. Manter `regras_aplicadas` como conjunto ordenado das regras realmente usadas; perguntas informativas laterais não contaminam as regras do pedido principal.

Critérios de aceite:

- nenhum turno válido fica sem resposta relevante;
- a resposta explica decisões e pendências sem expor dados proibidos;
- consulta de terceiro é recusada, mas o atendimento original continua no turno seguinte;
- protocolo não é duplicado por nova pergunta sobre o mesmo escalonamento;
- o último turno preserva decisão, valores, regras e protocolo corretos mesmo quando a pergunta final é lateral.

### Etapa 5 — Empacotamento e validação final

**Objetivo:** entregar uma imagem reproduzível e o repositório pronto para a execução única da banca.

Entregas:

1. Fixar todas as versões em `requirements.txt` e remover dependências não utilizadas que aumentem build ou imagem.
2. Confirmar que o Dockerfile somente instala dependências e copia `storage/`, `kb/` e `app/`; a ingestão não roda no build nem no start.
3. Construir a imagem sem depender de serviços externos além do índice já persistido e dos pacotes disponíveis durante a instalação prevista pela banca.
4. Medir build abaixo de 10 minutos, imagem abaixo de 4 GB e health check dentro de 60 segundos.
5. Executar os três casos de treino, revisar cada turno e comparar os campos finais com `esperado.json`.
6. Exercitar cenários adicionais manuais de isolamento de sessão, reset, anexo no primeiro turno, MCP v1/v2, terceiro, documento inválido e falha temporária de dependência.
7. Revisar `git diff` e `git status`, garantindo que `storage/` está incluído e que `.env`, credenciais e artefatos temporários não estão no pacote.

Critérios de aceite:

- os três casos de treino atingem os desfechos esperados, inclusive valores, regras e presença/ausência de protocolo;
- as verificações objetivas não encontram resposta curta, eco literal, CPF, CID ou carteirinha de terceiro;
- a imagem sobe somente com as quatro variáveis permitidas;
- o ZIP extraído contém tudo que o `docker build` precisa.

## 4. Regras de decisão e invariantes

1. Nenhuma decisão final será emitida sem categoria documental confirmada e dados mínimos exigidos pela norma.
2. `PENDENTE_DOCUMENTO` preserva o caso aberto para complementação; não equivale a negativa.
3. Documento inválido não é automaticamente despesa não coberta.
4. Toda aritmética usa `Decimal` e arredondamento explícito para duas casas.
5. Valores e categoria vêm do documento, ainda que o usuário forneça números diferentes no texto.
6. Cadastro e utilização vêm do MCP, ainda que o usuário faça estimativas.
7. Norma posterior com vigência aplicável prevalece sobre dispositivo substituído; número maior não implica maior atualidade.
8. Escalonamento humano abre protocolo pelo MCP e nunca informa valor estimado de reembolso.
9. A carteirinha da sessão não pode ser trocada. Dados de dependente, cônjuge ou titular diferente não serão consultados nem revelados.
10. Guardrails serão aplicados antes da serialização e novamente na resposta final como defesa em profundidade.

## 5. Estratégia de verificação

A implementação será verificada em camadas, sempre começando pelo menor teste relevante:

- funções puras de cálculo, vigência, fusão/reranking e sanitização;
- parsing Pydantic das respostas documentais e das duas versões do MCP;
- persistência e isolamento do checkpointer;
- recuperação dos dispositivos usados nos três gabaritos;
- fluxo completo de cada caso de treino, turno a turno;
- build e execução real do container contra o MCP fornecido.

Os gabaritos de treino são referências de regressão, não regras hardcoded. Nenhuma carteirinha, valor, frase de usuário ou sequência específica dos casos será incorporada à lógica de produção.

## 6. Definição de pronto

O desafio estará pronto para entrega quando:

- a arquitetura obrigatória estiver visível no código e funcional;
- os contratos HTTP e Pydantic forem cumpridos em todos os caminhos;
- o índice híbrido estiver construído e commitado em `storage/`;
- cálculos e decisões respeitarem a autoridade de cada fonte;
- os três casos de treino passarem com atendimento relevante em todos os turnos;
- não houver vazamento de CPF, CID, hipótese diagnóstica ou dado de terceiro;
- Docker cumprir tempo de build, tamanho, porta e tempo de inicialização;
- o repositório não contiver segredo e estiver pronto para compactação e envio.

O envio será feito por quem detém acesso ao e-mail, para `rodolfo.uchida@triggo.ai`, com o assunto obrigatório `[Desafio Final Bootcamp 2026]`, até 21/08.
