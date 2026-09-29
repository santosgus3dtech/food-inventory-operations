from datetime import date

PRODUCT_UPDATES = (
    {
        "published_on": date(2026, 9, 29),
        "label": "Mais recente",
        "tone": "new",
        "title": "Busca detalhada na Auditoria",
        "summary": ("Os administradores agora encontram alterações específicas com mais rapidez."),
        "items": (
            "A busca pode ser combinada com usuário, unidade, ação e tipo de registro.",
            "Também é possível limitar os resultados por data inicial e final.",
            "A paginação mantém todos os filtros escolhidos.",
            "A lista continua respeitando as permissões de cada usuário.",
        ),
        "impact": "Facilita investigar uma alteração sem percorrer todo o histórico.",
    },
    {
        "published_on": date(2026, 9, 29),
        "label": "Novo",
        "tone": "new",
        "title": "Planejamento semanal do cardápio",
        "summary": (
            "O cardápio agora possui uma tabela semanal para escolher os pratos de cada dia "
            "e refeição."
        ),
        "items": (
            "Cada nova semana começa com a tabela em branco.",
            "Administradores podem adicionar, trocar ou remover o prato de cada refeição.",
            "As opções são reunidas automaticamente a partir dos PDFs enviados.",
            "A lista completa de pratos aparece separada por tipo de refeição.",
            "Funcionários podem consultar o planejamento definido para a semana.",
        ),
        "impact": "Facilita montar e consultar o cardápio sem precisar copiar os pratos dos PDFs.",
    },
    {
        "published_on": date(2026, 9, 29),
        "label": "Novo",
        "tone": "new",
        "title": "Registros de descarte com fotos",
        "summary": (
            "A equipe agora pode documentar os descartes de cada unidade com imagens "
            "e uma explicação."
        ),
        "items": (
            "Cada registro informa unidade, data, observação e responsável.",
            "É possível anexar até oito fotos de uma só vez.",
            "As fotos ficam protegidas e visíveis somente para usuários da unidade.",
            "O envio gera um registro na Auditoria.",
        ),
        "impact": "Cria uma evidência organizada para acompanhar o que foi descartado e por quê.",
    },
    {
        "published_on": date(2026, 9, 29),
        "label": "Novo",
        "tone": "new",
        "title": "Cardápios semanais como referência",
        "summary": (
            "As refeições planejadas agora podem ser consultadas no próprio sistema, "
            "organizadas por dia e tipo de refeição."
        ),
        "items": (
            "Todos os usuários podem consultar o cardápio da semana e o histórico.",
            "Administradores podem enviar um ou vários cardápios em PDF.",
            "O sistema lê período, nutricionista e refeições quando reconhece a tabela.",
            "O PDF original permanece disponível para consulta e conferência.",
        ),
        "impact": "A equipe encontra a referência das refeições sem procurar arquivos separados.",
    },
    {
        "published_on": date(2026, 9, 28),
        "label": "Novo",
        "tone": "new",
        "title": "Catálogo PNAE e compras mais seguras",
        "summary": (
            "A lista oficial de alimentos de 2026 agora faz parte do sistema e ajuda "
            "a conferir as compras antes de colocá-las no estoque."
        ),
        "items": (
            "112 itens do Catálogo PNAE estão disponíveis para consulta.",
            "53 alimentos que ainda não existiam foram adicionados com estoque zerado.",
            "A nota fiscal mostra quando um produto pertence ao catálogo.",
            "Produtos fora da lista precisam de justificativa e aprovação de um administrador.",
        ),
        "impact": "Mais segurança para conferir se a compra está de acordo com a lista adotada.",
    },
    {
        "published_on": date(2026, 9, 28),
        "label": "Melhoria",
        "tone": "improvement",
        "title": "Notas fiscais mais rápidas de conferir",
        "summary": (
            "O recebimento de mercadorias ficou mais simples e reduz o preenchimento repetitivo."
        ),
        "items": (
            "É possível enviar vários arquivos XML ou PDF de uma vez.",
            "O sistema lê produtos, quantidades e valores encontrados na nota.",
            "Uma mesma nota pode ser distribuída entre várias unidades.",
            "Produtos já reconhecidos são sugeridos automaticamente nas próximas notas.",
            "Rascunhos podem ser excluídos, mantendo o registro de quem fez a exclusão.",
        ),
        "impact": "Menos trabalho manual e menor risco de lançar a mesma compra duas vezes.",
    },
    {
        "published_on": date(2026, 9, 28),
        "label": "Novo",
        "tone": "new",
        "title": "Histórico de preços dos alimentos",
        "summary": (
            "Os valores das notas fiscais agora ajudam a acompanhar quanto foi pago por "
            "cada alimento ao longo do tempo."
        ),
        "items": (
            "A ficha do alimento mostra o último, o menor e o maior preço registrado.",
            "Um gráfico permite acompanhar a variação do preço por data.",
            "Cada valor mantém o vínculo com o fornecedor e a nota fiscal de origem.",
        ),
        "impact": "Facilita comparar compras e perceber aumentos ou reduções de preço.",
    },
    {
        "published_on": date(2026, 9, 28),
        "label": "Novo",
        "tone": "new",
        "title": "Controle completo das movimentações",
        "summary": (
            "Toda mudança de quantidade pode ser registrada com o motivo correto e fica "
            "visível no histórico."
        ),
        "items": (
            "Entradas, consumos, perdas, devoluções, retornos, ajustes e estornos.",
            "Lote e validade podem ser informados quando forem necessários.",
            "O painel inicial destaca o que precisa de atenção.",
            "Itens disponíveis aparecem antes dos itens sem estoque.",
            "Produtos zerados ficam acinzentados e recebem a indicação “Sem estoque”.",
        ),
        "impact": "O saldo fica mais fácil de entender e cada alteração tem uma explicação.",
    },
    {
        "published_on": date(2026, 9, 28),
        "label": "Acesso",
        "tone": "access",
        "title": "Acesso mais simples para a equipe",
        "summary": (
            "Cada pessoa usa sua própria conta e vê somente as áreas necessárias para o "
            "seu trabalho."
        ),
        "items": (
            "Funcionários têm um menu simplificado com acesso direto ao estoque.",
            "Administradores podem visualizar o sistema como um funcionário.",
            "Novas contas aguardam a aprovação de um administrador.",
            "A opção “Lembrar de mim” mantém o acesso no dispositivo por até 30 dias.",
        ),
        "impact": "A equipe encontra as tarefas com mais facilidade e os acessos ficam controlados.",
    },
    {
        "published_on": date(2026, 9, 24),
        "label": "Lançamento",
        "tone": "launch",
        "title": "Primeira versão do Estoque FOODOPS",
        "summary": (
            "O sistema foi colocado no ar para reunir alimentos, unidades, usuários e "
            "quantidades em um só lugar."
        ),
        "items": (
            "Cadastro das 16 unidades da FOODOPS.",
            "Catálogo único de alimentos, categorias e embalagens.",
            "Controle de acesso por pessoa e por unidade.",
            "Registro permanente das alterações realizadas no sistema.",
        ),
        "impact": "As informações de estoque passam a ficar organizadas e disponíveis pela internet.",
    },
)
